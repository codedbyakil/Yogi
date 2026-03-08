#!/usr/bin/env python3
"""
IPTV M3U Playlist Processor
Processes and validates streaming channels while preserving DRM/protected content.
"""

import re
import requests
import asyncio
import aiohttp
from urllib.parse import urlparse
import sys
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass
from collections import OrderedDict
import logging
from concurrent.futures import ThreadPoolExecutor
import time

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Common headers to mimic a real IPTV player
DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': '*/*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Origin': 'https://m3u.8088y.fun',
    'Referer': 'https://m3u.8088y.fun/',
    'Sec-Ch-Ua': '"Not_A Brand";v="8", "Chromium";v="120"',
    'Sec-Ch-Ua-Mobile': '?0',
    'Sec-Ch-Ua-Platform': '"Windows"',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'same-origin',
    'Connection': 'keep-alive',
}

@dataclass
class ChannelBlock:
    """Represents a complete channel block from M3U playlist"""
    lines: List[str]
    extinf_line: str
    url: str
    metadata: Dict[str, str]
    is_protected: bool = False
    has_drm: bool = False

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
        
        return ChannelBlock(
            lines=lines,
            extinf_line=extinf_line,
            url=url,
            metadata=metadata,
            is_protected=is_protected,
            has_drm=has_drm
        )
    
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
    """Validates streams with intelligent handling of protected content"""
    
    def __init__(self, timeout: int = 5, max_workers: int = 10):
        self.timeout = timeout
        self.max_workers = max_workers
        self.session = None
    
    async def check_stream_async(self, url: str, is_protected: bool = False) -> Tuple[bool, int]:
        """
        Asynchronously check if a stream is accessible
        Returns (is_working, status_code)
        """
        if is_protected:
            # For protected streams, do a quick header check without requiring 200
            return await self._check_protected_stream(url)
        else:
            return await self._check_regular_stream(url)
    
    async def _check_regular_stream(self, url: str) -> Tuple[bool, int]:
        """Check regular streams (expect 200)"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.head(url, timeout=self.timeout, allow_redirects=True, headers=DEFAULT_HEADERS) as response:
                    # Accept 200 for regular streams
                    return response.status == 200, response.status
        except asyncio.TimeoutError:
            return False, 408
        except Exception as e:
            logger.debug(f"Stream check error: {e}")
            return False, 0
    
    async def _check_protected_stream(self, url: str) -> Tuple[bool, int]:
        """
        Check protected streams more leniently
        Accept 403, 401 as "possibly working" since they might work in players
        """
        try:
            async with aiohttp.ClientSession() as session:
                # Use GET with range request for partial content
                headers = {**DEFAULT_HEADERS, 'Range': 'bytes=0-1'}
                async with session.get(url, timeout=self.timeout, allow_redirects=True, headers=headers) as response:
                    # Protected streams might return 403/401 but still work in players
                    if response.status in [200, 206]:
                        return True, response.status
                    elif response.status in [403, 401]:
                        # Return as "working" for validation purposes
                        logger.debug(f"Protected stream {url} returned {response.status} - keeping")
                        return True, response.status
                    elif response.status < 500:  # Other client errors
                        return False, response.status
                    else:  # Server errors
                        return False, response.status
        except asyncio.TimeoutError:
            # Timeout might indicate working stream with slow response
            logger.debug(f"Protected stream {url} timed out - keeping")
            return True, 408
        except Exception as e:
            logger.debug(f"Protected stream check error: {e}")
            return False, 0
    
    async def validate_batch(self, blocks: List[ChannelBlock]) -> List[ChannelBlock]:
        """Validate multiple streams concurrently"""
        valid_blocks = []
        
        # Create tasks for all blocks
        tasks = []
        for block in blocks:
            if block.url:  # Only check blocks with URLs
                task = self.check_stream_async(block.url, block.is_protected)
                tasks.append((block, task))
        
        # Wait for all tasks to complete
        for block, task in tasks:
            try:
                is_working, status = await task
                if is_working:
                    valid_blocks.append(block)
                    logger.info(f"✓ {block.metadata.get('name', 'Unknown')} - {status}")
                else:
                    logger.warning(f"✗ {block.metadata.get('name', 'Unknown')} - Status {status}")
            except Exception as e:
                logger.error(f"Error checking {block.url}: {e}")
                # Keep the block if we can't verify it's dead
                valid_blocks.append(block)
        
        return valid_blocks

class PlaylistProcessor:
    """Main playlist processor with deduplication and optimization"""
    
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
            logger.info(f"Found {len(blocks)} channels")
            
            # Remove duplicates
            logger.info("Removing duplicates...")
            blocks = self._remove_duplicates(blocks)
            logger.info(f"{len(blocks)} channels after deduplication")
            
            # Validate streams
            logger.info("Validating streams...")
            valid_blocks = await self.validator.validate_batch(blocks)
            logger.info(f"{len(valid_blocks)} working channels after validation")
            
            # Keep only highest quality if multiple variants exist
            logger.info("Optimizing quality selection...")
            final_blocks = self._keep_highest_quality(valid_blocks)
            logger.info(f"{len(final_blocks)} channels after quality optimization")
            
            # Generate final playlist
            logger.info("Generating final playlist...")
            playlist = self.parser.generate_playlist(final_blocks)
            
            return playlist
            
        except Exception as e:
            logger.error(f"Processing error: {e}")
            return None
    
    def _download_playlist(self, url: str) -> Optional[str]:
        """Download playlist with retry logic and proper headers"""
        for attempt in range(3):
            try:
                session = requests.Session()
                
                # First try with browser headers
                response = session.get(
                    url, 
                    timeout=15,
                    headers=DEFAULT_HEADERS,
                    allow_redirects=True
                )
                
                # If that fails with 403, try with alternative headers
                if response.status_code == 403:
                    logger.info("Got 403, trying with alternative headers...")
                    alt_headers = {
                        'User-Agent': 'VLC/3.0.18 LibVLC/3.0.18',
                        'Accept': '*/*',
                        'Connection': 'keep-alive',
                    }
                    response = session.get(url, timeout=15, headers=alt_headers, allow_redirects=True)
                
                response.raise_for_status()
                return response.text
                
            except requests.RequestException as e:
                logger.warning(f"Download attempt {attempt + 1} failed: {e}")
                if attempt < 2:  # Don't sleep on last attempt
                    time.sleep(3)
        
        # Final attempt: try with curl-like headers
        try:
            logger.info("Final attempt with curl-like headers...")
            curl_headers = {
                'User-Agent': 'curl/8.4.0',
                'Accept': '*/*',
            }
            response = requests.get(url, timeout=15, headers=curl_headers, allow_redirects=True)
            response.raise_for_status()
            return response.text
        except:
            pass
            
        return None
    
    def _remove_duplicates(self, blocks: List[ChannelBlock]) -> List[ChannelBlock]:
        """Remove duplicate channels based on URL"""
        seen_urls = OrderedDict()
        unique_blocks = []
        
        for block in blocks:
            if block.url and block.url not in seen_urls:
                seen_urls[block.url] = True
                unique_blocks.append(block)
        
        return unique_blocks
    
    def _keep_highest_quality(self, blocks: List[ChannelBlock]) -> List[ChannelBlock]:
        """
        Keep highest quality when multiple variants of same channel exist
        """
        quality_map = {}
        
        # Group by channel name (or tvg-id)
        for block in blocks:
            channel_name = block.metadata.get('tvg-id', block.metadata.get('name', ''))
            
            if channel_name not in quality_map:
                quality_map[channel_name] = block
            else:
                # Simple quality selection (prioritize HD/FHD/UHD in name)
                current = quality_map[channel_name]
                if 'UHD' in block.extinf_line and 'UHD' not in current.extinf_line:
                    quality_map[channel_name] = block
                elif 'FHD' in block.extinf_line and 'FHD' not in current.extinf_line and 'UHD' not in current.extinf_line:
                    quality_map[channel_name] = block
                elif 'HD' in block.extinf_line and 'HD' not in current.extinf_line and 'FHD' not in current.extinf_line and 'UHD' not in current.extinf_line:
                    quality_map[channel_name] = block
        
        return list(quality_map.values())

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
        logger.info(f"Playlist size: {len(playlist.splitlines())} lines")
    else:
        logger.error("Failed to process playlist")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
