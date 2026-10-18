"""
Hybrid HDR10+DV Processing Module

Inspired by Sparkys Hybrid HDR10+DV script for Unshakled.

VT Version made by MasterOfKay

This module handles the hybrid mode feature which downloads both HDR10 and Dolby Vision tracks,
extracts DV metadata/RPU from the DV track (lowest quality), and injects it into the HDR10 track
(highest quality) to create a hybrid HDR10+DV stream.
"""

import json
import logging
import os
import random
import re
import subprocess
from pathlib import Path


class Hybrid:
    def __init__(self, hdr10_track, dv_track, temp_dir, service_name=None, audio_track=None):
        """
        Initialize hybrid processing for HDR10 and Dolby Vision tracks.
        
        Args:
            hdr10_track: VideoTrack object with HDR10 content (high quality)
            dv_track: VideoTrack object with Dolby Vision content (low quality, for metadata only)
            temp_dir: Directory for temporary files
            service_name: Name of the streaming service (optional)
        """
        self.log = logging.getLogger("hybrid")
        self.hdr10_track = hdr10_track
        self.dv_track = dv_track
        self.audio_track = audio_track
        self.temp_dir = Path(temp_dir)
        self.service_name = service_name
        
        # Detect AV1 transport and Netflix AV1 HDR10+ signaling.
        hdr10_codec = (hdr10_track.codec or '').lower()
        service_key = (service_name or '').lower()
        self.is_av1 = 'av01' in hdr10_codec or 'av1' in hdr10_codec
        self.is_netflix_av1_hdr10plus = (
            service_key in ('netflix', 'nf')
            and self.is_av1
            and 'hdr10plus' in hdr10_codec
        )
        self.use_unstable_tools = self.is_netflix_av1_hdr10plus
        
        # File names for processing
        self.rpu_file = "RPU.bin"
        self.hdr_type = "HDR10"
        
        # Set output file extension based on codec
        if self.is_av1:
            self.output_file = "AV1-DV.ivf"  # AV1 in IVF container
            self.log.info("HYBRIDPLUS Mode: AV1 HDR10+ with H.265 DV metadata")
        else:
            self.output_file = "HDR10-DV.hevc"

        # Resolve tool variants once per run.
        # HEVC DV extraction/edit should stay on stable dovi_tool.
        # AV1 injection can use unstable dovi_tool when available.
        self.dovi_tool_path_hevc = self._find_dovi_tool(prefer_unstable=False)
        self.dovi_tool_path_av1 = self._find_dovi_tool(prefer_unstable=self.use_unstable_tools)
        self.hdr10plus_tool_path = self._find_hdr10plus_tool(
            prefer_unstable=self.is_av1,
            force_unstable=self.is_av1,
        )
        if self.dovi_tool_path_hevc:
            self.log.info("Using dovi_tool binary (HEVC pipeline): %s", self.dovi_tool_path_hevc)
        if self.is_av1 and self.dovi_tool_path_av1:
            self.log.info("Using dovi_tool binary (AV1 injection): %s", self.dovi_tool_path_av1)
        if self.hdr10plus_tool_path:
            self.log.info("Using hdr10plus_tool binary: %s", self.hdr10plus_tool_path)
        if self.is_av1 and not self.use_unstable_tools:
            self.log.info(
                "AV1 detected without Netflix hdr10plus codec marker; using stable tools. codec=%s",
                hdr10_codec,
            )
            
        # Legacy compatibility
        self.hevc_file = self.output_file
        self.hdr10plus_to_dv = False
        self.hdr10plus_file = "HDR10Plus.json"
        
        # Get resolution info for display
        self.resolution = f"{hdr10_track.height}p" if hdr10_track.height else "Unknown"
        
        self.log.info("=" * 60)
        self.log.info("HDR10+DV Hybrid Processing (%s)", self.resolution)
        self.log.info("=" * 60)
        
        # Validate tracks are downloaded
        if not hdr10_track._location or not os.path.exists(hdr10_track._location):
            raise ValueError(f"HDR10 track was not downloaded before injection: {hdr10_track.id}")
        if not dv_track._location or not os.path.exists(dv_track._location):
            raise ValueError(f"DV track was not downloaded before injection: {dv_track.id}")
            
        # Check if already processed
        final_output = self.temp_dir / self.output_file
        if final_output.exists():
            self.log.info("✓ Hybrid track already injected")
            return
            
        # Start processing
        try:
            self.process()
        except Exception as e:
            self.log.error("Hybrid processing failed: %s", str(e))
            raise
            
    def process(self):
        """Main processing pipeline for hybrid tracks."""
        # Always start with a clean RPU workspace so no previous title can leak metadata.
        self._clear_rpu_intermediates()

        # Extract streams from downloaded tracks
        if self.is_av1:
            self.extract_stream(self.hdr10_track._location, "AV1")
        else:
            self.extract_stream(self.hdr10_track._location, "HDR10")
        self.extract_stream(self.dv_track._location, "DV")
        
        # Extract RPU from DV stream
        self.extract_rpu()

        # Use untouched RPU when extraction needed the fallback path.
        if (self.temp_dir / "RPU_UNT.bin").exists():
            self.rpu_file = "RPU_UNT.bin"

        # Apply dynamic luminance metadata (L6) and active area metadata (L5)
        # before final injection into the HDR10 base layer.
        self.level_6()
        if not self.is_av1:
            self.level_5(self.hdr10_track._location)
        
        # Inject DV metadata into HDR10/AV1 stream
        if self.is_av1:
            self.inject_rpu_av1()
        else:
            self.inject_rpu()
        
        self.log.info("✓ Injection Completed")
        
        # Cleanup temporary files
        self.cleanup()
        
    def extract_stream(self, input_path, stream_type):
        """Extract raw stream from container using FFmpeg."""
        # Determine output format based on stream type
        if stream_type == "AV1":
            # Use IVF container format for AV1 (standard AV1 container)
            output = self.temp_dir / f"{stream_type}.ivf"
            codec_args = ["-c:v", "copy", "-f", "ivf"]
        else:
            output = self.temp_dir / f"{stream_type}.hevc"
            codec_args = ["-f", "hevc", "-c:v", "copy"]
        
        if output.exists():
            self.log.info("%s stream already extracted", stream_type)
            return
            
        self.log.info("Extracting %s stream...", stream_type)
        
        # Find ffmpeg executable
        ffmpeg = self._find_ffmpeg()
        if not ffmpeg:
            raise EnvironmentError("ffmpeg not found. Please install ffmpeg for hybrid mode.")
        
        try:
            subprocess.run(
                [
                    ffmpeg,
                    "-nostdin",
                    "-loglevel", "error",
                    "-i", str(input_path)
                ] + codec_args + [
                    "-y",
                    str(output)
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True
            )
            self.log.info("Extracted %s stream", stream_type)
        except subprocess.CalledProcessError as e:
            output.unlink(missing_ok=True)
            self.log.error("Failed extracting %s stream: %s", stream_type, e.stderr.decode())
            raise
            
    def extract_rpu(self, untouched=False):
        """Extract RPU (Reference Processing Unit) from Dolby Vision stream."""
        rpu_name = "RPU_UNT.bin" if untouched else "RPU.bin"
        rpu_path = self.temp_dir / rpu_name

        # Force fresh extraction every run to avoid stale RPU reuse.
        rpu_path.unlink(missing_ok=True)
            
        self.log.info("Extracting%s RPU from Dolby Vision stream...", ' untouched' if untouched else '')
        
        # Check if dovi_tool is available
        dovi_tool = self._get_dovi_tool_hevc()
        if not dovi_tool:
            raise EnvironmentError("dovi_tool not found. Please install dovi_tool for hybrid mode.")
            
        # Build command
        cmd = [dovi_tool]
        if not untouched:
            cmd.extend(["-m", "3"])
        cmd.extend([
            "extract-rpu",
            str(self.temp_dir / "DV.hevc"),
            "-o",
            str(rpu_path)
        ])
        
        try:
            subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True
            )
            self.log.info("Extracted%s RPU", ' untouched' if untouched else '')
        except subprocess.CalledProcessError as e:
            rpu_path.unlink(missing_ok=True)
            stderr = e.stderr.decode()
            
            # Handle specific error cases
            if b"MAX_PQ_LUMINANCE" in e.stderr:
                self.log.warning("Retrying with untouched RPU extraction...")
                self.extract_rpu(untouched=True)
            elif b"Invalid PPS index" in e.stderr:
                raise ValueError("Dolby Vision track appears to be corrupt") from e
            else:
                self.log.error("Failed extracting RPU: %s", stderr)
                raise
                
    def level_6(self):
        """Edit RPU Level 6 metadata values using luminance extracted from RPU."""
        self.log.info("Reading RPU luminance metadata for L6...")

        dovi_tool = self._get_dovi_tool_hevc()
        if not dovi_tool:
            raise EnvironmentError("dovi_tool not found")

        try:
            info_result = subprocess.run(
                [
                    dovi_tool,
                    "info",
                    "-i",
                    str(self.temp_dir / self.rpu_file),
                    "-s",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            self.log.error("Failed reading RPU metadata for L6: %s", e.stderr)
            raise ValueError("Failed reading RPU metadata for L6 values") from e

        max_cll = None
        max_fall = None
        max_mdl = None
        min_mdl = None

        for line in info_result.stdout.splitlines():
            if "RPU content light level (L1):" in line and "MaxCLL:" in line:
                try:
                    parts = line.split("MaxCLL:", 1)[1].split(",")
                    max_cll = int(float(parts[0].strip().split()[0]))
                    if len(parts) > 1 and "MaxFALL:" in parts[1]:
                        max_fall = int(float(parts[1].split("MaxFALL:", 1)[1].strip().split()[0]))
                except (ValueError, IndexError):
                    pass
            elif "RPU mastering display:" in line:
                try:
                    mastering = line.split(":", 1)[1].strip()
                    min_lum, max_lum = mastering.split("/")[0], mastering.split("/")[1].split(" ")[0]
                    min_mdl = int(float(min_lum) * 10000)
                    max_mdl = int(float(max_lum))
                except (ValueError, IndexError):
                    pass

        if any(v is None for v in (max_cll, max_fall, max_mdl, min_mdl)):
            self.log.warning("Could not parse full L6 luminance from RPU, using safe defaults")
            max_cll = 0 if max_cll is None else max_cll
            max_fall = 0 if max_fall is None else max_fall
            max_mdl = 1000 if max_mdl is None else max_mdl
            min_mdl = 1 if min_mdl is None else min_mdl

        self.log.info("Editing RPU Level 6 values...")

        level6_json = self.temp_dir / "L6.json"
        level6_data = {
            "level6": {
                "remove_cmv4": False,
                "remove_mapping": False,
                "max_display_mastering_luminance": max_mdl,
                "min_display_mastering_luminance": min_mdl,
                "max_content_light_level": max_cll,
                "max_frame_average_light_level": max_fall,
            }
        }
        
        with open(level6_json, "w", encoding="utf-8") as f:
            json.dump(level6_data, f, indent=3)
            
        rpu_l6_path = self.temp_dir / "RPU_L6.bin"

        # Force regeneration for the current title.
        rpu_l6_path.unlink(missing_ok=True)
            
        try:
            subprocess.run(
                [
                    dovi_tool,
                    "editor",
                    "-i", str(self.temp_dir / self.rpu_file),
                    "-j", str(level6_json),
                    "-o", str(rpu_l6_path)
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True
            )
            self.log.info("Edited RPU Level 6 values")
            self.rpu_file = "RPU_L6.bin"
        except subprocess.CalledProcessError as e:
            rpu_l6_path.unlink(missing_ok=True)
            self.log.error("Failed editing RPU: %s", e.stderr.decode())
            raise

    def level_5(self, input_video):
        """Generate Level 5 active area metadata via crop detection."""
        rpu_l5_path = self.temp_dir / "RPU_L5.bin"
        # Force regeneration for the current title.
        rpu_l5_path.unlink(missing_ok=True)

        ffprobe = self._find_ffprobe()
        ffmpeg = self._find_ffmpeg()
        dovi_tool = self._get_dovi_tool_hevc()
        if not ffprobe or not ffmpeg or not dovi_tool:
            self.log.warning("Missing ffprobe/ffmpeg/dovi_tool for L5 crop detection, skipping")
            return

        try:
            duration_result = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "json",
                    str(input_video),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            duration_info = json.loads(duration_result.stdout)
            duration = float(duration_info["format"]["duration"])

            stream_result = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height",
                    "-of",
                    "json",
                    str(input_video),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            stream_info = json.loads(stream_result.stdout)
            original_width = int(stream_info["streams"][0]["width"])
            original_height = int(stream_info["streams"][0]["height"])
        except (subprocess.CalledProcessError, ValueError, KeyError, IndexError, json.JSONDecodeError) as e:
            self.log.warning("Could not probe stream for L5 crop detection: %s", e)
            return

        crop_results = []
        sample_count = 10
        random_times = sorted(random.uniform(0, max(duration, 0.1)) for _ in range(sample_count))

        for timestamp in random_times:
            crop_result = subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-nostdin",
                    "-loglevel",
                    "info",
                    "-ss",
                    f"{timestamp:.2f}",
                    "-i",
                    str(input_video),
                    "-vf",
                    "cropdetect=round=2",
                    "-vframes",
                    "10",
                    "-f",
                    "null",
                    "-",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            crop_match = re.search(
                r"crop=(\d+):(\d+):(\d+):(\d+)",
                (crop_result.stdout or "") + (crop_result.stderr or ""),
            )
            if crop_match:
                width = int(crop_match.group(1))
                height = int(crop_match.group(2))
                x = int(crop_match.group(3))
                y = int(crop_match.group(4))
                left = x
                top = y
                right = original_width - width - x
                bottom = original_height - height - y
                crop_results.append((left, top, right, bottom))

        if not crop_results:
            self.log.info("No active area crop data detected, skipping L5")
            return

        crop_counts = {}
        for crop in crop_results:
            crop_counts[crop] = crop_counts.get(crop, 0) + 1
        left, top, right, bottom = max(crop_counts, key=lambda crop: crop_counts[crop])

        if left == 0 and top == 0 and right == 0 and bottom == 0:
            return

        l5_json_path = self.temp_dir / "L5.json"
        l5_data = {
            "active_area": {
                "crop": False,
                "presets": [
                    {
                        "id": 0,
                        "left": left,
                        "right": right,
                        "top": top,
                        "bottom": bottom,
                    }
                ],
                "edits": {"all": 0},
            }
        }

        with open(l5_json_path, "w", encoding="utf-8") as f:
            json.dump(l5_data, f, indent=3)

        try:
            subprocess.run(
                [
                    dovi_tool,
                    "editor",
                    "-i",
                    str(self.temp_dir / self.rpu_file),
                    "-j",
                    str(l5_json_path),
                    "-o",
                    str(rpu_l5_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            self.rpu_file = "RPU_L5.bin"
            self.log.info(
                "Edited RPU Level 5 active area (left=%s top=%s right=%s bottom=%s)",
                left,
                top,
                right,
                bottom,
            )
        except subprocess.CalledProcessError as e:
            rpu_l5_path.unlink(missing_ok=True)
            self.log.warning("Failed editing RPU Level 5 active area: %s", e.stderr.decode())
            
    def inject_rpu(self):
        """Inject Dolby Vision RPU metadata into HDR10 stream."""
        output = self.temp_dir / self.output_file
        
        if output.exists():
            self.log.info("RPU already injected")
            return
            
        self.log.info("Injecting Dolby Vision metadata into %s stream...", self.hdr_type)
        
        dovi_tool = self._get_dovi_tool_hevc()
        if not dovi_tool:
            raise EnvironmentError("dovi_tool not found")
        
        try:
            subprocess.run(
                [
                    dovi_tool,
                    "inject-rpu",
                    "-i", str(self.temp_dir / "HDR10.hevc"),
                    "--rpu-in", str(self.temp_dir / self.rpu_file),
                    "-o", str(output)
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True
            )
            self.log.info("Injected Dolby Vision metadata into %s stream", self.hdr_type)
        except subprocess.CalledProcessError as e:
            output.unlink(missing_ok=True)
            self.log.error("Failed injecting RPU: %s", e.stderr.decode())
            raise
    
    def inject_rpu_av1(self):
        """Inject Dolby Vision RPU metadata into AV1 stream (HYBRIDPLUS mode)."""
        output = self.temp_dir / self.output_file
        
        if output.exists():
            self.log.info("RPU already injected into AV1 stream")
            return
            
        self.log.info("Injecting Dolby Vision metadata into AV1 HDR10+ stream...")
        self.log.warning("Note: AV1 DV injection is experimental and requires dovi_tool 2.1+")
        self.log.warning("As of late 2024, dovi_tool may not fully support AV1 DV injection yet")
        
        dovi_tool = self._get_dovi_tool_av1()
        if not dovi_tool:
            raise EnvironmentError("dovi_tool not found")
        
        try:
            av1_input = self.temp_dir / "AV1.ivf"
            
            # Try to inject RPU into AV1 stream using IVF format
            # Use mode 2 (FEL to MEL conversion) which is typical for hybrid workflows
            subprocess.run(
                [
                    dovi_tool,
                    "-m", "2",
                    "inject-rpu",
                    "-i", str(av1_input),
                    "--rpu-in", str(self.temp_dir / self.rpu_file),
                    "-o", str(output)
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True
            )
            self.log.info("✓ Successfully injected Dolby Vision metadata into AV1 HDR10+ stream")
        except subprocess.CalledProcessError as e:
            output.unlink(missing_ok=True)
            error_msg = e.stderr.decode()
            
            # Provide detailed error information
            self.log.error("Failed injecting RPU into AV1: %s", error_msg)
            
            if "not supported" in error_msg.lower() or "invalid" in error_msg.lower():
                self.log.error("="*60)
                self.log.error("AV1 DOLBY VISION INJECTION NOT SUPPORTED")
                self.log.error("="*60)
                self.log.error("The current version of dovi_tool does not support AV1 DV injection.")
                self.log.error("AV1 DV support is still experimental/in development.")
                self.log.error("")
                self.log.error("Options:")
                self.log.error("1. Use regular HYBRID mode with H.265 HDR10 + H.265 DV instead")
                self.log.error("2. Wait for dovi_tool to add full AV1 DV support")
                self.log.error("3. Download the latest dovi_tool: https://github.com/quietvoid/dovi_tool/releases")
                self.log.error("="*60)
            
            raise
            
    def cleanup(self):
        """Clean up temporary files."""
        temp_files = [
            "HDR10.hevc",
            "AV1.ivf",
            "DV.hevc",
            self.rpu_file,
            "RPU.bin",
            "RPU_UNT.bin",
            "RPU_L5.bin",
            "RPU_L6.bin",
            "L5.json",
            "L6.json"
        ]
        
        for filename in temp_files:
            filepath = self.temp_dir / filename
            filepath.unlink(missing_ok=True)

    def _clear_rpu_intermediates(self):
        """Remove all RPU intermediates so each run uses current-title metadata only."""
        for filename in ["RPU.bin", "RPU_UNT.bin", "RPU_L5.bin", "RPU_L6.bin", "L5.json", "L6.json"]:
            (self.temp_dir / filename).unlink(missing_ok=True)
            
    def _find_ffmpeg(self):
        """Find ffmpeg executable."""
        import shutil
        
        # Try common names
        names = ["ffmpeg", "ffmpeg.exe"]
        for name in names:
            tool = shutil.which(name)
            if tool:
                return tool
                
        # Check in binaries folder
        binaries_dir = Path(__file__).parent.parent.parent / "binaries"
        for name in names:
            tool_path = binaries_dir / name
            if tool_path.exists():
                return str(tool_path)
                
        return None

    def _get_dovi_tool_hevc(self):
        """Get dovi_tool for HEVC-based DV steps (extract/info/editor/inject)."""
        return self.dovi_tool_path_hevc or self._find_dovi_tool(prefer_unstable=False)

    def _get_dovi_tool_av1(self):
        """Get dovi_tool for AV1 injection. Prefer unstable build when HYBRIDPLUS requires it."""
        return (
            self.dovi_tool_path_av1
            or self._find_dovi_tool(prefer_unstable=self.use_unstable_tools)
            or self._get_dovi_tool_hevc()
        )
    
    def _find_dovi_tool(self, prefer_unstable=False):
        """Find dovi_tool executable. AV1/HYBRIDPLUS can prefer unstable build."""
        import shutil
        
        # Prefer patched unstable builds for AV1 workflows.
        if prefer_unstable:
            names = [
                "dovi_tool-unstable",
                "dovi_tool-unstable.exe",
                "dovi_tool",
                "dovi_tool.exe",
            ]
        else:
            names = [
                "dovi_tool",
                "dovi_tool.exe",
                "dovi_tool-unstable",
                "dovi_tool-unstable.exe",
            ]

        # Try PATH first.
        for name in names:
            tool = shutil.which(name)
            if tool:
                return tool
                
        # Then check local binaries folder.
        binaries_dir = Path(__file__).parent.parent.parent / "binaries"
        for name in names:
            tool_path = binaries_dir / name
            if tool_path.exists():
                return str(tool_path)
                
        return None

    def _find_hdr10plus_tool(self, prefer_unstable=False, force_unstable=False):
        """Find hdr10plus_tool executable. AV1/HYBRIDPLUS can prefer unstable build."""
        import shutil

        if force_unstable:
            names = [
                "hdr10plus_tool-unstable",
                "hdr10plus_tool-unstable.exe",
            ]
        elif prefer_unstable:
            names = [
                "hdr10plus_tool-unstable",
                "hdr10plus_tool-unstable.exe",
                "hdr10plus_tool",
                "hdr10plus_tool.exe",
            ]
        else:
            names = [
                "hdr10plus_tool",
                "hdr10plus_tool.exe",
                "hdr10plus_tool-unstable",
                "hdr10plus_tool-unstable.exe",
            ]

        for name in names:
            tool = shutil.which(name)
            if tool:
                return tool

        binaries_dir = Path(__file__).parent.parent.parent / "binaries"
        for name in names:
            tool_path = binaries_dir / name
            if tool_path.exists():
                return str(tool_path)

        return None

    def _find_ffprobe(self):
        """Find ffprobe executable."""
        import shutil

        names = ["ffprobe", "ffprobe.exe"]
        for name in names:
            tool = shutil.which(name)
            if tool:
                return tool

        binaries_dir = Path(__file__).parent.parent.parent / "binaries"
        for name in names:
            tool_path = binaries_dir / name
            if tool_path.exists():
                return str(tool_path)

        return None
        
    def get_output_path(self):
        """Get the path to the final hybrid output file."""
        return self.temp_dir / self.output_file
