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
        sealed_writer=None,
        release_when_idle: bool = False,
        warmup_sec: float = 0.0,
        warmup_frames: int = 1,
        tuning_mode: str = "standard",
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
            sealed_writer: Optional SealedWriter. When it holds a public key,
                captures are sealed so the payload cannot read them back.
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
        self.release_when_idle = release_when_idle
        # "standard": the sensor's normal tuning. "noir": greyworld white
        # balance, for a module with no IR-cut filter. "alternate": swap per
        # photo -- one frame keeps the infrared look, the next is balanced to
        # natural colour. Same hardware sees both; only the processing turns.
        self.tuning_mode = tuning_mode if tuning_mode in ("standard", "noir", "alternate") else "standard"
        self._active_variant: Optional[str] = None
        self._capture_index = 0
        self.warmup_sec = warmup_sec
        self.warmup_frames = max(0, warmup_frames)

        self._initialized: bool = False
        # Whether the sensor is currently streaming. The camera object stays
        # configured either way; only the pipeline is stopped, which is what
        # costs power and what takes 13 ms to restart.
        self._streaming: bool = False
        
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
        
        from common.sealedwriter import SealedWriter
        self._writer = sealed_writer or SealedWriter(enabled=False)

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
            if not self._open_camera(self.desired_variant(self._capture_index)):
                return False

            if self.release_when_idle:
                # Nothing will be captured for a while; do not sit streaming
                # until then. Measured on a Pi Zero 2 W: an open, idle camera
                # keeps the SoC about 2 C warmer than a stopped one.
                self._stop_streaming()

            self._initialized = True
            logger.info(f"Camera initialized at {self.resolution}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to initialize camera: {e}")
            return False
    
    def _start_streaming(self) -> bool:
        """
        Bring the sensor back up, and let exposure settle before anyone looks.

        Measured on a Pi Zero 2 W with an IMX219: stop 14 ms, start 13 ms,
        first frame 131 ms against 67 ms steady state. End to end the release
        costs about 160 ms per capture against an interval measured in tens of
        seconds.

        The warm-up exists for a narrower reason than first assumed.
        Auto-exposure was expected to reconverge from scratch after a restart;
        measured in a lit scene it does not. libcamera keeps its exposure state
        across stop()/start() while the configuration is unchanged, and frame
        zero came back within 0.1% of the settled reference across three
        trials. Restarting does not cost an exposure.

        What it cannot do is adapt while stopped. If the scene changes during
        the idle period -- the balloon climbs out of cloud into direct sun --
        the first frame after the restart is metered for the old scene. One
        discarded frame gives the loop a cycle to react, which is cheap next to
        a ruined image, and is why the default is one frame rather than none.
        """
        if self._streaming or self._camera is None:
            return True
        try:
            self._camera.start()
            self._streaming = True

            if self.warmup_sec > 0:
                time.sleep(self.warmup_sec)

            # Throw away the first frames outright. Sleeping alone does not
            # guarantee the pipeline has produced a properly exposed frame,
            # and discarding is cheap next to a bad image.
            for _ in range(self.warmup_frames):
                try:
                    self._camera.capture_array()
                except Exception:
                    break

            logger.debug(
                f"Camera streaming resumed ({self.warmup_sec:.2f}s + "
                f"{self.warmup_frames} discarded frames)")
            return True
        except Exception as e:
            logger.error(f"Could not restart the camera: {e}")
            self._streaming = False
            return False

    def _stop_streaming(self) -> None:
        """Stop the pipeline but keep the configuration, so restarting is cheap."""
        if not self._streaming or self._camera is None:
            return
        try:
            self._camera.stop()
            self._streaming = False
            logger.debug("Camera streaming stopped to save power")
        except Exception as e:
            # A camera that will not stop is a nuisance, not a reason to stop
            # flying. Leave it streaming and carry on.
            logger.warning(f"Could not stop the camera: {e}")

    def release(self) -> None:
        """
        Release the sensor until the next capture.

        Called by the flight loop once a capture is done. Does nothing unless
        release_when_idle is set, so the default behaviour is unchanged.
        """
        if self.release_when_idle and not self.simulate:
            self._stop_streaming()

    @property
    def streaming(self) -> bool:
        return self._streaming


    @staticmethod
    def noir_tuning_file(sensor_model: str) -> str:
        """Tuning file for a sensor with no IR-cut filter.

        A NoIR camera reports the same sensor ID as the filtered one, so this
        cannot be detected -- it has to be configured. Without it, infrared
        leaking into all three colour channels drags auto white balance
        towards magenta and every image comes out purple. The _noir tunings
        use greyworld white balance, which ignores the sensor's calibrated
        illuminant tables (wrong for a filterless sensor) and simply balances
        the scene.
        """
        return f"{sensor_model}_noir.json"

    def _noir_tuning(self):
        """Load the NoIR tuning for whatever sensor is attached, or None.

        The sensor is identified in a subprocess, deliberately. Asking
        libcamera in this process -- global_camera_info() -- creates its
        process-wide camera manager, and the manager reads the tuning
        environment exactly once, at creation. Ask first and the tuning
        passed to Picamera2() afterwards is silently ignored: the camera
        runs the standard tuning while every flag says otherwise. That is
        not hypothetical; it is how this payload shipped purple "natural"
        frames with 'variant=noir' in the log.
        """
        try:
            import subprocess
            import sys as _sys
            probe = subprocess.run(
                [_sys.executable, "-c",
                 "from picamera2 import Picamera2;"
                 "info = Picamera2.global_camera_info();"
                 "print(info[0]['Model'] if info else '')"],
                capture_output=True, text=True, timeout=30,
            )
            model = probe.stdout.strip().splitlines()[-1] if probe.stdout.strip() else ""
            if not model:
                logger.warning("No camera model detected; using standard tuning")
                return None
            return Picamera2.load_tuning_file(self.noir_tuning_file(model))
        except Exception as exc:  # missing tuning file, no camera, ...
            logger.warning("NoIR tuning unavailable (%s); using standard", exc)
            return None


    def desired_variant(self, capture_index: int) -> str:
        """Which tuning the given capture should use.

        Alternation starts with the infrared look ("standard" tuning on a
        filterless sensor) and swaps every photo. Scheduling is untouched:
        the same captures happen at the same times, they just take turns.
        """
        if self.tuning_mode == "noir":
            return "noir"
        if self.tuning_mode == "alternate":
            return "standard" if capture_index % 2 == 0 else "noir"
        return "standard"

    # The white-balance gains behind the infrared render. Measured from what
    # the standard tuning's AWB converges to on a filterless sensor: red near
    # unity, blue pushed hard, which is exactly the purple cast. Fixed gains
    # rather than live AWB so every infrared frame is rendered identically --
    # a consistent false-colour is what makes frames comparable.
    IR_RENDER_GAINS = (1.1, 2.5)

    def _open_camera(self, variant: str) -> bool:
        """Create and start the camera, then apply the starting render.

        Any mode that ever wants natural colour from a filterless sensor
        needs the greyworld tuning loaded here: libcamera's camera manager
        is a process singleton, so a second Picamera2 constructed with a
        different tuning silently keeps the first one's. The tuning is
        therefore chosen by mode, once, and the per-photo variants differ
        only in white-balance controls.
        """
        tuning = None
        if self.tuning_mode in ("noir", "alternate"):
            tuning = self._noir_tuning()
            if tuning is None and self.tuning_mode == "noir":
                variant = "standard"

        self._camera = Picamera2(tuning=tuning) if tuning else Picamera2()

        config = self._camera.create_still_configuration(
            main={"size": self.resolution, "format": "RGB888"},
            buffer_count=2
        )
        self._camera.configure(config)
        self._apply_camera_settings()
        self._camera.start()
        self._streaming = True
        self._active_variant = variant
        time.sleep(0.5)  # Allow auto-exposure to settle
        if self.tuning_mode == "alternate":
            self._apply_variant(variant)
        return True

    def _apply_variant(self, variant: str) -> None:
        """Switch the render on the live camera.

        A control change, not a camera rebuild: with the greyworld tuning
        loaded, natural colour is AWB, and the infrared look is fixed gains.
        Two subtleties, both measured on the bench:

        - AwbEnable alone does not undo manual gains; the documented release
          is ColourGains of zero, after which the algorithm runs again.
        - AWB takes seconds to reconverge after release, so the release
          happens right after the infrared capture (_release_to_awb), not
          before the natural one. The minutes between captures do the
          converging, and the natural frame needs no settling at all.
        """
        if self._camera is None:
            return
        try:
            if variant == "noir":
                # Normally already released by _release_to_awb; this is the
                # fallback path (first capture, or an interrupted sequence).
                self._camera.set_controls({
                    "AwbEnable": True,
                    "ColourGains": (0.0, 0.0),
                })
                time.sleep(1.2)  # reconvergence margin
            else:
                self._camera.set_controls({
                    "AwbEnable": False,
                    "ColourGains": self.IR_RENDER_GAINS,
                })
                # Fixed gains land within a few frames.
                time.sleep(0.4)
            self._active_variant = variant
        except Exception as exc:
            logger.warning("Could not switch render to %s: %s", variant, exc)

    def _release_to_awb(self) -> None:
        """Hand white balance back to AWB immediately after an infrared frame.

        Zero gains are the documented release. Done here so the idle time
        between captures -- half a minute in flight -- is when reconvergence
        happens, invisibly. If the camera is released between captures the
        pipeline cannot converge while stopped; the warm-up frames after
        restart absorb most of that, and a slightly warm first natural frame
        is the accepted cost of the power saving.
        """
        if self._camera is None:
            return
        try:
            self._camera.set_controls({
                "AwbEnable": True,
                "ColourGains": (0.0, 0.0),
            })
            self._active_variant = "noir"
        except Exception as exc:
            logger.warning("Could not release white balance to AWB: %s", exc)

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

        # Which look this photo gets. In alternate mode every capture flips
        # between the infrared render and the colour-balanced one; rebuilding
        # the camera is only needed when the variant actually changes.
        if not self.simulate:
            variant = self.desired_variant(self._capture_index)
            self._capture_index += 1
            if self._active_variant is None:
                # A camera that was opened without going through
                # _open_camera. Adopt it as-is rather than disturb it.
                self._active_variant = variant
            elif variant != self._active_variant:
                self._apply_variant(variant)

        # Bring the sensor up if it was released after the last capture. This
        # is deliberately inside capture() rather than left to the caller: a
        # capture that silently returned a frame from a stopped pipeline would
        # be a very confusing bug.
        if not self.simulate and not self._streaming:
            if not self._start_streaming():
                return None

        try:
            if self.simulate:
                return self._simulate_capture(latitude, longitude, altitude)
            
            # Capture burst and select sharpest
            image = self._capture_burst()

            # An infrared frame leaves manual gains behind; hand white
            # balance straight back so AWB reconverges during the idle time
            # before the next (natural) capture instead of delaying it.
            if self.tuning_mode == "alternate" and self._active_variant == "standard":
                self._release_to_awb()

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
            filepath = self._writer.write(filepath, webp_data)
            
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
        filepath = self._writer.write(filepath, webp_data)
        
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
        
        # Sealed captures end in .rhs. Matching only *.webp meant the
        # retention sweep skipped every encrypted image, so storage would fill
        # over a long flight and capture would start failing.
        files = (glob.glob(os.path.join(self.storage_path, "*.webp"))
                 + glob.glob(os.path.join(self.storage_path, "*.webp.rhs")))
        
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
            if self._streaming:
                self._camera.stop()
                self._streaming = False
            self._camera.close()
            self._camera = None
        
        self._initialized = False
        logger.info("Camera closed")


# Alias for backward compatibility
CameraModule = Camera
