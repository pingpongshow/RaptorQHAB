//
//  OfflineMapManager.swift
//  RaptorHabMobile
//
//  Manages offline map tile caching using OpenStreetMap tiles
//  iOS version adapted from Mac app
//

import Foundation
import MapKit
import SQLite3

// MARK: - Offline Tile Overlay

class OfflineTileOverlay: MKTileOverlay {
    
    private let cache: TileCache
    
    init(cache: TileCache) {
        self.cache = cache
        super.init(urlTemplate: nil)
        self.canReplaceMapContent = true
        self.tileSize = CGSize(width: 256, height: 256)
        self.minimumZ = 1
        self.maximumZ = 18
    }
    
    override func url(forTilePath path: MKTileOverlayPath) -> URL {
        return URL(string: "https://tile.openstreetmap.org/\(path.z)/\(path.x)/\(path.y).png")!
    }
    
    override func loadTile(at path: MKTileOverlayPath, result: @escaping (Data?, Error?) -> Void) {
        // First, try to load from cache
        if let cachedData = cache.getTile(x: path.x, y: path.y, z: path.z) {
            result(cachedData, nil)
            return
        }
        
        // If not cached, try to download (if online)
        let url = URL(string: "https://tile.openstreetmap.org/\(path.z)/\(path.x)/\(path.y).png")!
        
        var request = URLRequest(url: url)
        request.setValue("RaptorHabMobile/1.0 (HAB Tracker)", forHTTPHeaderField: "User-Agent")
        
        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            if let data = data, error == nil {
                // Cache the tile for offline use
                self?.cache.saveTile(x: path.x, y: path.y, z: path.z, data: data)
                result(data, nil)
            } else {
                result(nil, error)
            }
        }.resume()
    }
}

// MARK: - Tile Cache (SQLite MBTiles format)

class TileCache {
    
    private var db: OpaquePointer?
    private let dbPath: URL
    private let queue = DispatchQueue(label: "com.raptorhabmobile.tilecache", qos: .utility)
    
    var tileCount: Int {
        var count = 0
        queue.sync {
            var stmt: OpaquePointer?
            if sqlite3_prepare_v2(db, "SELECT COUNT(*) FROM tiles", -1, &stmt, nil) == SQLITE_OK {
                if sqlite3_step(stmt) == SQLITE_ROW {
                    count = Int(sqlite3_column_int(stmt, 0))
                }
            }
            sqlite3_finalize(stmt)
        }
        return count
    }
    
    var cacheSize: Int64 {
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: dbPath.path) else {
            return 0
        }
        return attrs[.size] as? Int64 ?? 0
    }
    
    init(directory: URL) {
        self.dbPath = directory.appendingPathComponent("tiles.mbtiles")
        
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        
        if sqlite3_open(dbPath.path, &db) == SQLITE_OK {
            createTables()
        }
    }
    
    deinit {
        sqlite3_close(db)
    }
    
    private func createTables() {
        let createSQL = """
            CREATE TABLE IF NOT EXISTS tiles (
                zoom_level INTEGER NOT NULL,
                tile_column INTEGER NOT NULL,
                tile_row INTEGER NOT NULL,
                tile_data BLOB NOT NULL,
                PRIMARY KEY (zoom_level, tile_column, tile_row)
            );
            CREATE TABLE IF NOT EXISTS metadata (
                name TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE INDEX IF NOT EXISTS tiles_idx ON tiles (zoom_level, tile_column, tile_row);
        """
        
        var errMsg: UnsafeMutablePointer<CChar>?
        sqlite3_exec(db, createSQL, nil, nil, &errMsg)
        if let errMsg = errMsg {
            sqlite3_free(errMsg)
        }
    }
    
    func getTile(x: Int, y: Int, z: Int) -> Data? {
        var result: Data?
        
        // MBTiles uses TMS y-coordinate (flipped)
        let tmsY = (1 << z) - 1 - y
        
        queue.sync {
            var stmt: OpaquePointer?
            let query = "SELECT tile_data FROM tiles WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?"
            
            if sqlite3_prepare_v2(db, query, -1, &stmt, nil) == SQLITE_OK {
                sqlite3_bind_int(stmt, 1, Int32(z))
                sqlite3_bind_int(stmt, 2, Int32(x))
                sqlite3_bind_int(stmt, 3, Int32(tmsY))
                
                if sqlite3_step(stmt) == SQLITE_ROW {
                    if let blob = sqlite3_column_blob(stmt, 0) {
                        let size = sqlite3_column_bytes(stmt, 0)
                        result = Data(bytes: blob, count: Int(size))
                    }
                }
            }
            sqlite3_finalize(stmt)
        }
        
        return result
    }
    
    func saveTile(x: Int, y: Int, z: Int, data: Data) {
        // MBTiles uses TMS y-coordinate (flipped)
        let tmsY = (1 << z) - 1 - y
        
        queue.async { [weak self] in
            guard let self = self else { return }
            
            var stmt: OpaquePointer?
            let query = "INSERT OR REPLACE INTO tiles (zoom_level, tile_column, tile_row, tile_data) VALUES (?, ?, ?, ?)"
            
            if sqlite3_prepare_v2(self.db, query, -1, &stmt, nil) == SQLITE_OK {
                sqlite3_bind_int(stmt, 1, Int32(z))
                sqlite3_bind_int(stmt, 2, Int32(x))
                sqlite3_bind_int(stmt, 3, Int32(tmsY))
                _ = data.withUnsafeBytes { ptr in
                    sqlite3_bind_blob(stmt, 4, ptr.baseAddress, Int32(data.count), nil)
                }
                sqlite3_step(stmt)
            }
            sqlite3_finalize(stmt)
        }
    }
    
    func clearCache() {
        queue.async { [weak self] in
            sqlite3_exec(self?.db, "DELETE FROM tiles", nil, nil, nil)
        }
    }
}

// MARK: - Offline Map Manager

class OfflineMapManager: ObservableObject {
    
    static let shared = OfflineMapManager()
    
    @Published var isDownloading = false
    @Published var downloadProgress: Double = 0
    @Published var tileCount: Int = 0
    @Published var cacheSize: String = "0 MB"
    @Published var downloadedTiles: Int = 0
    @Published var totalTilesToDownload: Int = 0
    
    let cache: TileCache
    let tileOverlay: OfflineTileOverlay
    
    private var downloadTask: Task<Void, Never>?
    
    private init() {
        let cacheDir = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("RaptorHabMobile")
            .appendingPathComponent("MapCache")
        
        self.cache = TileCache(directory: cacheDir)
        self.tileOverlay = OfflineTileOverlay(cache: cache)
        
        updateStats()
    }
    
    func updateStats() {
        tileCount = cache.tileCount
        let bytes = cache.cacheSize
        cacheSize = ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
    }
    
    /// Download tiles for a region
    func downloadRegion(center: CLLocationCoordinate2D, radiusKm: Double, minZoom: Int = 5, maxZoom: Int = 15) {
        guard !isDownloading else { return }
        
        isDownloading = true
        downloadProgress = 0
        downloadedTiles = 0
        
        downloadTask = Task {
            await performDownload(center: center, radiusKm: radiusKm, minZoom: minZoom, maxZoom: maxZoom)
            
            await MainActor.run {
                self.isDownloading = false
                self.updateStats()
            }
        }
    }
    
    func cancelDownload() {
        downloadTask?.cancel()
        isDownloading = false
    }
    
    func clearCache() {
        cache.clearCache()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
            self.updateStats()
        }
    }
    
    private func performDownload(center: CLLocationCoordinate2D, radiusKm: Double, minZoom: Int, maxZoom: Int) async {
        var tilesToDownload: [(x: Int, y: Int, z: Int)] = []
        
        // Calculate tiles needed for each zoom level
        for z in minZoom...maxZoom {
            let tiles = tilesInRadius(center: center, radiusKm: radiusKm, zoom: z)
            tilesToDownload.append(contentsOf: tiles)
        }
        
        let total = tilesToDownload.count
        var completed = 0
        
        await MainActor.run {
            self.totalTilesToDownload = total
        }
        
        for tile in tilesToDownload {
            if Task.isCancelled { break }
            
            // Skip if already cached
            if cache.getTile(x: tile.x, y: tile.y, z: tile.z) != nil {
                completed += 1
                let currentCompleted = completed
                let currentProgress = Double(currentCompleted) / Double(total)
                await MainActor.run {
                    self.downloadProgress = currentProgress
                    self.downloadedTiles = currentCompleted
                }
                continue
            }
            
            // Download tile
            let url = URL(string: "https://tile.openstreetmap.org/\(tile.z)/\(tile.x)/\(tile.y).png")!
            var request = URLRequest(url: url)
            request.setValue("RaptorHabMobile/1.0 (HAB Tracker)", forHTTPHeaderField: "User-Agent")
            
            do {
                let (data, _) = try await URLSession.shared.data(for: request)
                cache.saveTile(x: tile.x, y: tile.y, z: tile.z, data: data)
            } catch {
                // Continue on error
            }
            
            completed += 1
            let currentCompleted = completed
            let currentProgress = Double(currentCompleted) / Double(total)
            
            await MainActor.run {
                self.downloadProgress = currentProgress
                self.downloadedTiles = currentCompleted
            }
            
            // Rate limit to be nice to OSM servers (100ms between requests)
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
    }
    
    private func tilesInRadius(center: CLLocationCoordinate2D, radiusKm: Double, zoom: Int) -> [(x: Int, y: Int, z: Int)] {
        var tiles: [(x: Int, y: Int, z: Int)] = []
        
        // Convert center to tile coordinates
        let centerTile = coordToTile(lat: center.latitude, lon: center.longitude, zoom: zoom)
        
        // Calculate how many tiles the radius covers at this zoom
        let metersPerTile = 156543.03 * cos(center.latitude * .pi / 180) / pow(2.0, Double(zoom)) * 256
        let tilesRadius = Int(ceil(radiusKm * 1000 / metersPerTile))
        
        // Get all tiles in the square around center
        for x in (centerTile.x - tilesRadius)...(centerTile.x + tilesRadius) {
            for y in (centerTile.y - tilesRadius)...(centerTile.y + tilesRadius) {
                if x >= 0 && y >= 0 && x < (1 << zoom) && y < (1 << zoom) {
                    tiles.append((x: x, y: y, z: zoom))
                }
            }
        }
        
        return tiles
    }
    
    private func coordToTile(lat: Double, lon: Double, zoom: Int) -> (x: Int, y: Int) {
        let n = pow(2.0, Double(zoom))
        let x = Int((lon + 180.0) / 360.0 * n)
        let latRad = lat * .pi / 180.0
        let y = Int((1.0 - asinh(tan(latRad)) / .pi) / 2.0 * n)
        return (x: x, y: y)
    }
    
    /// Estimate number of tiles for a download
    func estimateTiles(radiusKm: Double, minZoom: Int, maxZoom: Int, latitude: Double = 38.5) -> Int {
        var total = 0
        for z in minZoom...maxZoom {
            let metersPerTile = 156543.03 * cos(latitude * .pi / 180) / pow(2.0, Double(z)) * 256
            let tilesRadius = Int(ceil(radiusKm * 1000 / metersPerTile))
            let tilesPerSide = tilesRadius * 2 + 1
            total += tilesPerSide * tilesPerSide
        }
        return total
    }
    
    /// Estimate download size
    func estimateSize(tileCount: Int) -> String {
        // Average OSM tile is ~15KB
        let bytes = Int64(tileCount * 15_000)
        return ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
    }
}
