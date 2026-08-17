"""
RaptorHab Camera Module
IMX219 camera capture with burst mode and image overlay
"""

import os
import io
import time
import logging
import subprocess
from dataclasses import dataclass
from typing import Optional, Tuple, List
from datetime import datetime

logger = logging.getLogger(__name__)

# Try to import camera libraries
try:
    from picamera2 import Picamera2
    from picamera2.encoders import JpegEncoder
    PICAMERA2_AVAILABLE = True
except ImportError:
    PICAMERA2_AVAILABLE = False
    logger.warning("picamera2 not available")

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    logger.warning("PIL/Pillow not available")


@dataclass
class ImageInfo:
    """Information about a captured image"""
    image_id: int
    filepath: str
    width: int
    height: int
    size_bytes: int
    timestamp: int
    latitude: float = 0.0
    longitude: float = 0.0
    altitude: float = 0.0
    webp_data: Optional[bytes] = None


class Camera:
    """Camera capture with burst mode and overlay support"""
    
    # AWB mode mapping
    AWB_MODES = {
        0: 'auto',
        1: 'daylight',
        2: 'cloudy',
        3: 'tungsten',
        4: 'fluorescent',
        5: 'indoor',
        6: 'manual',
    }
    
    def __init__(
        self,
        resolution: Tuple[int, int] = (1280, 960),
        burst_count: int = 5,
        webp_quality: int = 75,
        overlay_enabled: bool = True,
        storage_path: str = "/home/pi/raptorhab/images",
        callsign: str = "RPHAB1",
        simulate: bool = False,
        simulation: bool = None,  # Alias for simulate
    ):
        """
        Initialize camera
        
        Args:
            resolution: Image resolution (width, height)
            burst_count: Number of images in burst for sharpness selection
            webp_quality: WebP compression quality (0-100)
            overlay_enabled: Add text overlay to images
            storage_path: Path to store captured images
            callsign: Callsign for overlay
            simulate: Enable simulation mode (alias: simulation)
            simulation: Alias for simulate
        """
        # Handle alias
        if simulation is not None:
            simulate = simulation
            
        self.resolution = resolution
        self.burst_count = burst_count
        self.webp_quality = webp_quality
        self.overlay_enabled = overlay_enabled
        self.storage_path = storage_path
        self.callsign = callsign
        self.simulate = simulate
        
        self._camera: Optional[Picamera2] = None
        self._image_counter: int = 0
        self._initialized: bool = False
        
        # Image adjustment settings (0-200 scale, 100 = neutral)
        self._brightness = 100  # 0=dark, 100=normal, 200=bright
        self._contrast = 100    # 0=low, 100=normal, 200=high
        self._saturation = 100  # 0=grayscale, 100=normal, 200=vivid
        self._sharpness = 100   # 0=soft, 100=normal, 200=sharp
        self._exposure_comp = 100  # 0=-2EV, 100=0EV, 200=+2EV
        self._awb_mode = 0      # 0=auto
        
        # Color gains for fixing red/pink tint (50-200 scale, 100 = no adjustment)
        self._red_gain = 100
        self._blue_gain = 100
        
        # Create storage directory
        os.makedirs(storage_path, exist_ok=True)
    
    def init(self) -> bool:
        """
        Initialize camera
        
        Returns:
            True on success
        """
        if self.simulate:
            logger.info("Camera in simulation mode")
            self._initialized = True
            return True
        
        if not PICAMERA2_AVAILABLE:
            logger.error("picamera2 not available")
            return False
        
        try:
            self._camera = Picamera2()
            
            # Configure for still image capture
            config = self._camera.create_still_configuration(
                main={"size": self.resolution, "format": "RGB888"},
                buffer_count=2
            )
            self._camera.configure(config)
            
            # Apply initial camera settings
            self._apply_camera_settings()
            
            # Start camera
            self._camera.start()
            time.sleep(0.5)  # Allow auto-exposure to settle
            
            self._initialized = True
            logger.info(f"Camera initialized at {self.resolution}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to initialize camera: {e}")
            return False
    
    def _apply_camera_settings(self):
        """Apply current image adjustment settings to camera"""
        if not self._camera or self.simulate:
            return
        
        try:
            controls = {}
            
            # Brightness: map 0-200 to -1.0 to 1.0
            controls['Brightness'] = (self._brightness - 100) / 100.0
            
            # Contrast: map 0-200 to 0.0 to 2.0
            controls['Contrast'] = self._contrast / 100.0
            
            # Saturation: map 0-200 to 0.0 to 2.0
            controls['Saturation'] = self._saturation / 100.0
            
            # Sharpness: map 0-200 to 0.0 to 2.0
            controls['Sharpness'] = self._sharpness / 100.0
            
            # Exposure compensation: map 0-200 to -2.0 to 2.0 EV
            controls['ExposureValue'] = (self._exposure_comp - 100) / 50.0
            
            # Color gains - only apply if not using auto AWB or if manually adjusted
            # Map 50-200 to 0.5-2.0 gain multiplier
            if self._red_gain != 100 or self._blue_gain != 100:
                red = self._red_gain / 100.0
                blue = self._blue_gain / 100.0
                controls['ColourGains'] = (red, blue)
                # Disable AWB when using manual gains
                controls['AwbEnable'] = False
                logger.debug(f"Manual color gains: red={red:.2f}, blue={blue:.2f}")
            
            self._camera.set_controls(controls)
            logger.debug(f"Applied camera settings: brightness={self._brightness}, contrast={self._contrast}, saturation={self._saturation}")
            
        except Exception as e:
            logger.warning(f"Failed to apply camera settings: {e}")
    
    def set_brightness(self, value: int) -> bool:
        """Set brightness (0-200, 100=normal)"""
        if not 0 <= value <= 200:
            return False
        self._brightness = value
        self._apply_camera_settings()
        logger.info(f"Brightness set to {value}")
        return True
    
    def set_contrast(self, value: int) -> bool:
        """Set contrast (0-200, 100=normal)"""
        if not 0 <= value <= 200:
            return False
        self._contrast = value
        self._apply_camera_settings()
        logger.info(f"Contrast set to {value}")
        return True
    
    def set_saturation(self, value: int) -> bool:
        """Set saturation (0-200, 100=normal)"""
        if not 0 <= value <= 200:
            return False
        self._saturation = value
        self._apply_camera_settings()
        logger.info(f"Saturation set to {value}")
        return True
    
    def set_sharpness(self, value: int) -> bool:
        """Set sharpness (0-200, 100=normal)"""
        if not 0 <= value <= 200:
            return False
        self._sharpness = value
        self._apply_camera_settings()
        logger.info(f"Sharpness set to {value}")
        return True
    
    def set_exposure_comp(self, value: int) -> bool:
        """Set exposure compensation (0-200, 100=0EV)"""
        if not 0 <= value <= 200:
            return False
        self._exposure_comp = value
        self._apply_camera_settings()
        logger.info(f"Exposure compensation set to {value}")
        return True
    
    def set_awb_mode(self, value: int) -> bool:
        """Set auto white balance mode (0=auto, 1=daylight, etc.)"""
        if value not in self.AWB_MODES:
            return False
        self._awb_mode = value
        self._apply_camera_settings()
        logger.info(f"AWB mode set to {self.AWB_MODES[value]}")
        return True
    
    def set_red_gain(self, value: int) -> bool:
        """Set red channel gain (50-200, 100=normal). Lower values reduce red/pink tint."""
        if not 50 <= value <= 200:
            return False
        self._red_gain = value
        self._apply_camera_settings()
        logger.info(f"Red gain set to {value} ({value/100:.2f}x)")
        return True
    
    def set_blue_gain(self, value: int) -> bool:
        """Set blue channel gain (50-200, 100=normal). Higher values can counteract red tint."""
        if not 50 <= value <= 200:
            return False
        self._blue_gain = value
        self._apply_camera_settings()
        logger.info(f"Blue gain set to {value} ({value/100:.2f}x)")
        return True
    
    def get_settings(self) -> dict:
        """Get current camera settings"""
        return {
            'brightness': self._brightness,
            'contrast': self._contrast,
            'saturation': self._saturation,
            'sharpness': self._sharpness,
            'exposure_comp': self._exposure_comp,
            'awb_mode': self._awb_mode,
            'awb_mode_name': self.AWB_MODES.get(self._awb_mode, 'auto'),
            'red_gain': self._red_gain,
            'blue_gain': self._blue_gain,
            'webp_quality': self.webp_quality,
        }
    
    def set_webp_quality(self, quality: int) -> bool:
        """Set WebP compression quality (1-100)"""
        if not 1 <= quality <= 100:
            return False
        self.webp_quality = quality
        logger.info(f"WebP quality set to {quality}")
        return True
    
    def capture(
        self,
        latitude: float = 0.0,
        longitude: float = 0.0,
        altitude: float = 0.0
    ) -> Optional[ImageInfo]:
        """
        Capture an image
        
        Args:
            latitude: GPS latitude for overlay
            longitude: GPS longitude for overlay
            altitude: GPS altitude for overlay
            
        Returns:
            ImageInfo or None on failure
        """
        if not self._initialized:
            logger.error("Camera not initialized")
            return None
        
        try:
            if self.simulate:
                return self._simulate_capture(latitude, longitude, altitude)
            
            # Capture burst and select sharpest
            image = self._capture_burst()
            
            if image is None:
                return None
            
            # Add overlay if enabled
            if self.overlay_enabled and PIL_AVAILABLE:
                image = self._add_overlay(image, latitude, longitude, altitude)
            
            # Convert to WebP
            webp_data = self._encode_webp(image)
            
            if webp_data is None:
                return None
            
            # Generate image ID and filepath
            self._image_counter += 1
            image_id = self._image_counter
            timestamp = int(time.time())
            
            filename = f"img_{image_id:05d}_{timestamp}.webp"
            filepath = os.path.join(self.storage_path, filename)
            
            # Save to disk
            with open(filepath, 'wb') as f:
                f.write(webp_data)
            
            info = ImageInfo(
                image_id=image_id,
                filepath=filepath,
                width=image.width,
                height=image.height,
                size_bytes=len(webp_data),
                timestamp=timestamp,
                latitude=latitude,
                longitude=longitude,
                altitude=altitude,
                webp_data=webp_data
            )
            
            logger.info(
                f"Image {image_id} captured: {info.width}x{info.height}, "
                f"{info.size_bytes} bytes"
            )
            
            return info
            
        except Exception as e:
            logger.error(f"Capture failed: {e}")
            return None
    
    def _capture_burst(self) -> Optional[Image.Image]:
        """
        Capture a burst of images and return the sharpest one
        
        Returns:
            PIL Image or None
        """
        if not PIL_AVAILABLE:
            # Single capture without PIL
            array = self._camera.capture_array()
            return Image.fromarray(array)
        
        images = []
        sharpness_scores = []
        
        for _ in range(self.burst_count):
            # Capture frame
            array = self._camera.capture_array()
            img = Image.fromarray(array)
            
            # Calculate sharpness (Laplacian variance)
            score = self._calculate_sharpness(img)
            
            images.append(img)
            sharpness_scores.append(score)
        
        # Select sharpest image
        best_idx = sharpness_scores.index(max(sharpness_scores))
        logger.debug(f"Selected image {best_idx + 1}/{self.burst_count} with sharpness {sharpness_scores[best_idx]:.1f}")
        
        return images[best_idx]
    
    def _calculate_sharpness(self, image: Image.Image) -> float:
        """
        Calculate image sharpness using Laplacian variance
        
        Args:
            image: PIL Image
            
        Returns:
            Sharpness score (higher is sharper)
        """
        try:
            # Convert to grayscale
            gray = image.convert('L')
            
            # Simple Laplacian approximation
            width, height = gray.size
            pixels = gray.load()
            
            variance = 0.0
            count = 0
            
            # Sample pixels for speed
            step = 4
            for y in range(step, height - step, step):
                for x in range(step, width - step, step):
                    # Laplacian = center * 4 - neighbors
                    lap = (
                        4 * pixels[x, y]
                        - pixels[x - 1, y]
                        - pixels[x + 1, y]
                        - pixels[x, y - 1]
                        - pixels[x, y + 1]
                    )
                    variance += lap * lap
                    count += 1
            
            return variance / count if count > 0 else 0.0
            
        except Exception:
            return 0.0
    
    def _add_overlay(
        self,
        image: Image.Image,
        latitude: float,
        longitude: float,
        altitude: float
    ) -> Image.Image:
        """
        Add text overlay to image
        
        Args:
            image: PIL Image
            latitude: GPS latitude
            longitude: GPS longitude
            altitude: GPS altitude in meters
            
        Returns:
            Image with overlay
        """
        try:
            draw = ImageDraw.Draw(image)
            
            # Try to use a monospace font
            font_size = 16
            try:
                font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", font_size)
            except IOError:
                font = ImageFont.load_default()
            
            # Build overlay text
            timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            
            # Handle None GPS values
            if latitude is not None and longitude is not None:
                gps_line = f"{latitude:.5f}, {longitude:.5f}"
            else:
                gps_line = "No GPS fix"
            
            if altitude is not None:
                alt_line = f"Alt: {altitude:.0f}m"
            else:
                alt_line = "Alt: ---"
            
            lines = [
                self.callsign,
                timestamp,
                gps_line,
                alt_line
            ]
            
            # Draw semi-transparent background
            text_height = font_size * len(lines) + 10
            box_width = 250
            
            # Create overlay with alpha
            overlay = Image.new('RGBA', image.size, (0, 0, 0, 0))
            overlay_draw = ImageDraw.Draw(overlay)
            
            # Black semi-transparent background
            overlay_draw.rectangle(
                [(5, 5), (box_width, text_height + 5)],
                fill=(0, 0, 0, 180)
            )
            
            # Draw text
            y = 8
            for line in lines:
                overlay_draw.text((10, y), line, font=font, fill=(255, 255, 255, 255))
                y += font_size + 2
            
            # Composite
            image = image.convert('RGBA')
            image = Image.alpha_composite(image, overlay)
            image = image.convert('RGB')
            
            return image
            
        except Exception as e:
            logger.warning(f"Failed to add overlay: {e}")
            return image
    
    def _encode_webp(self, image: Image.Image) -> Optional[bytes]:
        """
        Encode image as WebP
        
        Args:
            image: PIL Image
            
        Returns:
            WebP bytes or None
        """
        try:
            buffer = io.BytesIO()
            image.save(buffer, format='WEBP', quality=self.webp_quality)
            return buffer.getvalue()
        except Exception as e:
            logger.error(f"WebP encoding failed: {e}")
            return None
    
    def _simulate_capture(
        self,
        latitude: float,
        longitude: float,
        altitude: float
    ) -> ImageInfo:
        """Generate simulated image"""
        # Create a test pattern image
        width, height = self.resolution
        
        if PIL_AVAILABLE:
            image = Image.new('RGB', (width, height), color=(135, 206, 235))  # Sky blue
            draw = ImageDraw.Draw(image)
            
            # Add some visual elements
            draw.rectangle([(0, height // 2), (width, height)], fill=(34, 139, 34))  # Green ground
            draw.ellipse([(width // 4, height // 4), (width // 4 + 100, height // 4 + 100)], fill=(255, 255, 0))  # Sun
            
            # Add text
            try:
                font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
            except IOError:
                font = ImageFont.load_default()
            
            draw.text((width // 2 - 100, height // 2 - 20), "SIMULATION", font=font, fill=(255, 0, 0))
            
            # Add overlay if enabled
            if self.overlay_enabled:
                image = self._add_overlay(image, latitude, longitude, altitude)
            
            webp_data = self._encode_webp(image)
        else:
            # Minimal test data
            webp_data = b'\x00' * 1000
        
        self._image_counter += 1
        image_id = self._image_counter
        timestamp = int(time.time())
        
        filename = f"img_{image_id:05d}_{timestamp}.webp"
        filepath = os.path.join(self.storage_path, filename)
        
        # Save to disk
        with open(filepath, 'wb') as f:
            f.write(webp_data)
        
        return ImageInfo(
            image_id=image_id,
            filepath=filepath,
            width=width,
            height=height,
            size_bytes=len(webp_data),
            timestamp=timestamp,
            latitude=latitude,
            longitude=longitude,
            altitude=altitude,
            webp_data=webp_data
        )
    
    def get_image_count(self) -> int:
        """Get number of images captured"""
        return self._image_counter
    
    def cleanup_old_images(self, max_images: int = 100) -> int:
        """
        Remove oldest images to stay under limit
        
        Args:
            max_images: Maximum images to keep
            
        Returns:
            Number of images removed
        """
        import glob
        
        files = glob.glob(os.path.join(self.storage_path, "*.webp"))
        
        if len(files) <= max_images:
            return 0
        
        # Sort by modification time
        files.sort(key=os.path.getmtime)
        
        to_remove = files[:len(files) - max_images]
        removed = 0
        
        for filepath in to_remove:
            try:
                os.remove(filepath)
                removed += 1
            except OSError:
                pass
        
        logger.info(f"Cleaned up {removed} old images")
        return removed
    
    def close(self):
        """Close camera"""
        if self._camera:
            self._camera.stop()
            self._camera.close()
            self._camera = None
        
        self._initialized = False
        logger.info("Camera closed")


# Alias for backward compatibility
CameraModule = Camera
