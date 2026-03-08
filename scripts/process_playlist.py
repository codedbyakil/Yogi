#!/usr/bin/env python3
"""
IPTV M3U Playlist Processor
Focuses on live stream validation while preserving FM radio and media library content.
"""

import re
import requests
import asyncio
import aiohttp
from urllib.parse import urlparse, parse_qs
import sys
from typing import List, Dict, Any, Optional, Tuple, Set
from dataclasses import dataclass
from collections import OrderedDict
import logging
import time
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Headers to mimic a real IPTV player
DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': '*/*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
}

@dataclass
class ChannelBlock:
    """Represents a complete channel block from M3U playlist"""
    lines: List[str]
    extinf_line: str
    url: str
    metadata: Dict[str, str]
    channel_type: str = 'live'  # 'live', 'radio', 'media', 'unknown'
    is_protected: bool = False
    has_drm: bool = False
    url_hash: str = ''

class M3UParser:
    """Robust M3U parser that handles multi-line channel blocks"""
    
    def __init__(self):
        self.channel_blocks: List[ChannelBlock] = []
        self.header = "#EXTM3U\n"
        
    def parse(self, content: str) -> List[ChannelBlock]:
        """Parse M3U content into channel blocks"""
        lines = content.strip().split('\n')
        blocks = []
        current_block = []
        
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            
            # Skip empty lines and header
            if not line or line == "#EXTM3U":
                i += 1
                continue
            
            # Start of a new channel
            if line.startswith("#EXTINF:"):
                if current_block:
                    blocks.append(self._create_block(current_block))
                current_block = [line]
            elif current_block:
                current_block.append(line)
            
            i += 1
        
        # Add the last block
        if current_block:
            blocks.append(self._create_block(current_block))
        
        self.channel_blocks = blocks
        return blocks
    
    def _create_block(self, lines: List[str]) -> ChannelBlock:
        """Create a ChannelBlock from lines"""
        extinf_line = lines[0]
        
        # Find the URL (first line that doesn't start with #)
        url = ""
        for line in lines:
            if not line.startswith('#'):
                url = line
                break
        
        # Extract metadata from EXTINF
        metadata = self._parse_extinf(extinf_line)
        
        # Determine channel type
        channel_type = self._determine_channel_type(extinf_line, url, lines)
        
        # Check for DRM/protection indicators
        is_protected = any(
            'drm' in line.lower() or 
            'clearkey' in line.lower() or
            'token' in line.lower() or
            '?tk=' in line or
            'license' in line.lower() or
            '.mpd' in line.lower()
            for line in lines
        )
        
        has_drm = any(
            'clearkey' in line.lower() or
            'license' in line.lower()
            for line in lines
        )
        
        # Create a hash for duplicate detection
        url_hash = self._create_url_hash(url, metadata)
        
        return ChannelBlock(
            lines=lines,
            extinf_line=extinf_line,
            url=url,
            metadata=metadata,
            channel_type=channel_type,
            is_protected=is_protected,
            has_drm=has_drm,
            url_hash=url_hash
        )
    
    def _determine_channel_type(self, extinf_line: str, url: str, lines: List[str]) -> str:
        """Determine if channel is live TV, radio, or media library"""
        combined_text = (extinf_line + ' ' + url + ' ' + ' '.join(lines)).lower()
        
        # Check for radio/FM indicators
        radio_keywords = ['radio', 'fm ', ' fm', 'music', 'audio', 'listen', 'station']
        if any(keyword in combined_text for keyword in radio_keywords):
            return 'radio'
        
        # Check for media library/VOD indicators
        media_keywords = ['movie', 'series', 'vod', 'library', 'archive', 'catch-up', 'replay']
        if any(keyword in combined_text for keyword in media_keywords):
            return 'media'
        
        # Check URL patterns
        if '.mp3' in url or '.aac' in url or '.ogg' in url:
            return 'radio'
        
        if '.mp4' in url or '.mkv' in url or '.avi' in url:
            return 'media'
        
        # Default to live TV
        return 'live'
    
    def _create_url_hash(self, url: str, metadata: Dict[str, str]) -> str:
        """Create a hash for duplicate detection"""
        # Remove query parameters for URL comparison
        base_url = url.split('?')[0] if url else ''
        
        # Use tvg-id if available
        tvg_id = metadata.get('tvg-id', '')
        channel_name = metadata.get('name', '')
        
        # Combine identifiers
        hash_string = f"{base_url}|{tvg_id}|{channel_name}"
        return str(hash(hash_string))
    
    def _parse_extinf(self, extinf_line: str) -> Dict[str, str]:
        """Parse EXTINF line to extract metadata"""
        metadata = {}
        
        # Extract tvg-id, tvg-name, etc.
        pattern = r'([a-zA-Z0-9_-]+)="([^"]*)"'
        matches = re.findall(pattern, extinf_line)
        
        for key, value in matches:
            metadata[key] = value
        
        # Extract channel name (after the last comma)
        if ',' in extinf_line:
            metadata['name'] = extinf_line.split(',')[-1].strip()
        
        return metadata
    
    def generate_playlist(self, blocks: List[ChannelBlock]) -> str:
        """Generate M3U playlist from channel blocks"""
        playlist = [self.header]
        
        for block in blocks:
            playlist.extend(block.lines)
        
        return '\n'.join(playlist)

class StreamValidator:
    """Validates streams with focus on live TV"""
    
    def __init__(self, timeout: int = 8, max_workers: int = 20):
        self.timeout = timeout
        self.max_workers = max_workers
        self.session = None
    
    async def check_stream_async(self, block: ChannelBlock) -> Tuple[bool, int]:
        """
        Asynchronously check if a stream is accessible
        Different validation rules based on channel type
        """
        url = block.url
        channel_type = block.channel_type
        is_protected = block.is_protected
        
        # Radio stations - quick check
        if channel_type == 'radio':
            return await self._check_radio_stream(url)
        
        # Media library - more lenient
        elif channel_type == 'media':
            return await self._check_media_stream(url)
        
        # Live TV - strict check for non-protected, lenient for protected
        else:
            if is_protected:
                return await self._check_protected_stream(url)
            else:
                return await self._check_live_stream(url)
    
    async def _check_live_stream(self, url: str) -> Tuple[bool, int]:
        """Check live TV streams (strict)"""
        try:
            async with aiohttp.ClientSession() as session:
                # Use HEAD request for live streams
                async with session.head(url, timeout=self.timeout, allow_redirects=True, headers=DEFAULT_HEADERS) as response:
                    # Live streams should return 200
                    if response.status == 200:
                        return True, response.status
                    elif response.status in [301, 302, 307, 308]:
                        # Redirects might be okay
                        return True, response.status
                    else:
                        return False, response.status
        except asyncio.TimeoutError:
            return False, 408
        except Exception as e:
            logger.debug(f"Live stream check error: {e}")
            return False, 0
    
    async def _check_radio_stream(self, url: str) -> Tuple[bool, int]:
        """Check radio streams (quick header check)"""
        try:
            async with aiohttp.ClientSession() as session:
                # Quick HEAD request for radio streams
                async with session.head(url, timeout=3, allow_redirects=True, headers=DEFAULT_HEADERS) as response:
                    # Radio streams often return 200 or 206
                    if response.status in [200, 206]:
                        return True, response.status
                    elif response.status in [301, 302, 307, 308]:
                        return True, response.status
                    else:
                        return False, response.status
        except:
            # Keep radio stations if they fail (might be temporarily offline)
            return True, 0
    
    async def _check_media_stream(self, url: str) -> Tuple[bool, int]:
        """Check media library streams (lenient)"""
        try:
            async with aiohttp.ClientSession() as session:
                # Use range request for media files
                headers = {**DEFAULT_HEADERS, 'Range': 'bytes=0-1'}
                async with session.get(url, timeout=5, allow_redirects=True, headers=headers) as response:
                    # Media files often return 206 Partial Content
                    if response.status in [200, 206]:
                        return True, response.status
                    elif response.status in [301, 302, 307, 308]:
                        return True, response.status
                    else:
                        return False, response.status
        except:
            # Keep media library content (might be large files)
            return True, 0
    
    async def _check_protected_stream(self, url: str) -> Tuple[bool, int]:
        """Check protected/DASH streams (very lenient)"""
        try:
            async with aiohttp.ClientSession() as session:
                # Minimal check for protected streams
                headers = {**DEFAULT_HEADERS, 'Range': 'bytes=0-1'}
                async with session.get(url, timeout=3, allow_redirects=True, headers=headers) as response:
                    # Accept most responses for protected streams
                    if response.status < 500:  # Any non-server error
                        return True, response.status
                    else:
                        return False, response.status
        except:
            # Always keep protected streams if we can't verify
            return True, 0
    
    async def validate_batch(self, blocks: List[ChannelBlock]) -> List[ChannelBlock]:
        """Validate multiple streams concurrently"""
        valid_blocks = []
        tasks = []
        
        # Create tasks for all blocks with URLs
        for block in blocks:
            if block.url:
                task = self.check_stream_async(block)
                tasks.append((block, task))
        
        # Wait for all tasks to complete
        for block, task in tasks:
            try:
                is_working, status = await task
                if is_working:
                    valid_blocks.append(block)
                    if block.channel_type == 'live':
                        logger.info(f"✓ LIVE: {block.metadata.get('name', 'Unknown')} - {status}")
                    elif block.channel_type == 'radio':
                        logger.info(f"📻 RADIO: {block.metadata.get('name', 'Unknown')} - {status}")
                    elif block.channel_type == 'media':
                        logger.info(f"🎬 MEDIA: {block.metadata.get('name', 'Unknown')} - {status}")
                else:
                    if block.channel_type == 'live':
                        logger.warning(f"✗ LIVE DEAD: {block.metadata.get('name', 'Unknown')} - Status {status}")
                        # Don't add to valid_blocks (remove dead live streams)
                    else:
                        # Keep radio and media even if check fails
                        valid_blocks.append(block)
                        logger.info(f"✓ KEPT {block.channel_type.upper()}: {block.metadata.get('name', 'Unknown')} (unverifiable)")
            except Exception as e:
                logger.error(f"Error checking {block.url}: {e}")
                # Keep the block if we can't verify (except live streams)
                if block.channel_type != 'live':
                    valid_blocks.append(block)
        
        return valid_blocks

class PlaylistProcessor:
    """Main playlist processor"""
    
    def __init__(self):
        self.parser = M3UParser()
        self.validator = StreamValidator()
    
    async def process(self, source_url: str) -> Optional[str]:
        """Main processing function"""
        try:
            # Download source playlist
            logger.info(f"Downloading playlist from {source_url}")
            content = self._download_playlist(source_url)
            if not content:
                logger.error("Failed to download playlist")
                return None
            
            # Parse into blocks
            logger.info("Parsing playlist...")
            blocks = self.parser.parse(content)
            logger.info(f"Found {len(blocks)} total items")
            
            # Count by type
            live_count = sum(1 for b in blocks if b.channel_type == 'live')
            radio_count = sum(1 for b in blocks if b.channel_type == 'radio')
            media_count = sum(1 for b in blocks if b.channel_type == 'media')
            logger.info(f"Types: {live_count} live, {radio_count} radio, {media_count} media")
            
            # Remove duplicates
            logger.info("Removing duplicates...")
            blocks = self._remove_duplicates(blocks)
            logger.info(f"{len(blocks)} items after deduplication")
            
            # Validate streams (focus on live streams)
            logger.info("Validating streams (strict for live, lenient for radio/media)...")
            valid_blocks = await self.validator.validate_batch(blocks)
            
            # Final count by type
            final_live = sum(1 for b in valid_blocks if b.channel_type == 'live')
            final_radio = sum(1 for b in valid_blocks if b.channel_type == 'radio')
            final_media = sum(1 for b in valid_blocks if b.channel_type == 'media')
            
            logger.info(f"Final: {final_live} live, {final_radio} radio, {final_media} media")
            
            # Generate final playlist
            logger.info("Generating final playlist...")
            playlist = self.parser.generate_playlist(valid_blocks)
            
            return playlist
            
        except Exception as e:
            logger.error(f"Processing error: {e}")
            return None
    
    def _download_playlist(self, url: str) -> Optional[str]:
        """Download playlist with retry logic"""
        for attempt in range(3):
            try:
                response = requests.get(
                    url, 
                    timeout=15,
                    headers=DEFAULT_HEADERS,
                    allow_redirects=True
                )
                response.raise_for_status()
                return response.text
            except requests.RequestException as e:
                logger.warning(f"Download attempt {attempt + 1} failed: {e}")
                if attempt < 2:
                    time.sleep(3)
        return None
    
    def _remove_duplicates(self, blocks: List[ChannelBlock]) -> List[ChannelBlock]:
        """Remove duplicate channels based on URL hash"""
        seen_hashes = set()
        unique_blocks = []
        
        for block in blocks:
            if block.url_hash not in seen_hashes:
                seen_hashes.add(block.url_hash)
                unique_blocks.append(block)
            else:
                logger.debug(f"Removed duplicate: {block.metadata.get('name', 'Unknown')}")
        
        return unique_blocks

async def main():
    """Main entry point"""
    source_url = "https://m3u.8088y.fun/testing.m3u"
    output_file = "main.m3u"
    
    processor = PlaylistProcessor()
    playlist = await processor.process(source_url)
    
    if playlist:
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(playlist)
        logger.info(f"Playlist saved to {output_file}")
        
        # Print summary
        lines = playlist.splitlines()
        channels = [l for l in lines if l.startswith('#EXTINF:')]
        logger.info(f"Final playlist: {len(channels)} channels, {len(lines)} total lines")
    else:
        logger.error("Failed to process playlist")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
