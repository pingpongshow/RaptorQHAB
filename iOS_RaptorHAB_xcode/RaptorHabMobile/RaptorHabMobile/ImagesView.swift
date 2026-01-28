//
//  ImagesView.swift
//  RaptorHabMobile
//
//  Display of received images from the balloon payload
//

import SwiftUI

// MARK: - Image Wrapper for Identifiable

struct ImageItem: Identifiable {
    let id: UInt16
    let data: Data
}

struct ImagesView: View {
    @EnvironmentObject var groundStation: GroundStationManager
    @State private var selectedImageItem: ImageItem?
    
    private let columns = [
        GridItem(.adaptive(minimum: 150, maximum: 200))
    ]
    
    var body: some View {
        ScrollView {
            if groundStation.completedImages.isEmpty && groundStation.pendingImages.isEmpty {
                VStack(spacing: 16) {
                    Image(systemName: "photo.stack")
                        .font(.system(size: 48))
                        .foregroundColor(.secondary)
                    Text("No Images")
                        .font(.headline)
                    Text("Images received from the payload will appear here")
                        .font(.subheadline)
                        .foregroundColor(.secondary)
                        .multilineTextAlignment(.center)
                }
                .padding(40)
            } else {
                VStack(alignment: .leading, spacing: 16) {
                    // Completed images
                    if !groundStation.completedImages.isEmpty {
                        Section {
                            LazyVGrid(columns: columns, spacing: 12) {
                                ForEach(Array(groundStation.completedImages.keys.sorted()), id: \.self) { imageId in
                                    if let imageData = groundStation.completedImages[imageId],
                                       let uiImage = UIImage(data: imageData) {
                                        CompletedImageThumbnail(
                                            imageId: imageId,
                                            image: uiImage,
                                            isSelected: selectedImageItem?.id == imageId
                                        )
                                        .onTapGesture {
                                            selectedImageItem = ImageItem(id: imageId, data: imageData)
                                        }
                                    }
                                }
                            }
                        } header: {
                            Text("Completed Images")
                                .font(.headline)
                        }
                    }
                    
                    // Pending images
                    if !groundStation.pendingImages.isEmpty {
                        Section {
                            LazyVGrid(columns: columns, spacing: 12) {
                                ForEach(Array(groundStation.pendingImages.keys.sorted()), id: \.self) { imageId in
                                    if let pending = groundStation.pendingImages[imageId] {
                                        PendingImageThumbnail(pending: pending)
                                    }
                                }
                            }
                        } header: {
                            Text("In Progress")
                                .font(.headline)
                        }
                    }
                }
                .padding()
            }
        }
        .navigationTitle("Images")
        .sheet(item: $selectedImageItem) { item in
            if let uiImage = UIImage(data: item.data) {
                ImageDetailView(imageId: item.id, image: uiImage)
            }
        }
    }
}

// MARK: - Completed Image Thumbnail

struct CompletedImageThumbnail: View {
    let imageId: UInt16
    let image: UIImage
    let isSelected: Bool
    
    var body: some View {
        VStack {
            Image(uiImage: image)
                .resizable()
                .aspectRatio(contentMode: .fill)
                .frame(height: 120)
                .clipped()
                .cornerRadius(8)
                .overlay(
                    RoundedRectangle(cornerRadius: 8)
                        .stroke(isSelected ? Color.blue : Color.clear, lineWidth: 3)
                )
            
            Text("Image #\(imageId)")
                .font(.caption)
                .foregroundColor(.secondary)
        }
    }
}

// MARK: - Pending Image Thumbnail

struct PendingImageThumbnail: View {
    let pending: PendingImage
    
    var body: some View {
        VStack {
            ZStack {
                RoundedRectangle(cornerRadius: 8)
                    .fill(Color.secondary.opacity(0.2))
                    .frame(height: 120)
                
                VStack(spacing: 8) {
                    ProgressView(value: pending.progress, total: 100)
                        .progressViewStyle(CircularProgressViewStyle())
                    
                    Text("\(Int(pending.progress))%")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
            }
            
            Text("Image #\(pending.id)")
                .font(.caption)
                .foregroundColor(.secondary)
            
            if let meta = pending.metadata {
                Text("\(pending.symbols.count)/\(meta.numSourceSymbols) symbols")
                    .font(.caption2)
                    .foregroundColor(.secondary)
            }
        }
    }
}

// MARK: - Image Detail View

struct ImageDetailView: View {
    let imageId: UInt16
    let image: UIImage
    
    @Environment(\.dismiss) var dismiss
    @State private var scale: CGFloat = 1.0
    
    var body: some View {
        NavigationView {
            GeometryReader { geometry in
                ScrollView([.horizontal, .vertical], showsIndicators: false) {
                    Image(uiImage: image)
                        .resizable()
                        .aspectRatio(contentMode: .fit)
                        .frame(
                            width: geometry.size.width * scale,
                            height: geometry.size.height * scale
                        )
                        .gesture(
                            MagnificationGesture()
                                .onChanged { value in
                                    scale = min(max(value, 0.5), 4.0)
                                }
                        )
                        .gesture(
                            TapGesture(count: 2)
                                .onEnded {
                                    withAnimation {
                                        scale = scale > 1 ? 1 : 2
                                    }
                                }
                        )
                }
            }
            .navigationTitle("Image #\(imageId)")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .navigationBarLeading) {
                    Button("Close") {
                        dismiss()
                    }
                }
                
                ToolbarItem(placement: .navigationBarTrailing) {
                    ShareLink(item: Image(uiImage: image), preview: SharePreview("Image #\(imageId)", image: Image(uiImage: image)))
                }
            }
        }
    }
}

#Preview {
    ImagesView()
        .environmentObject(GroundStationManager())
}
