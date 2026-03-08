#!/usr/bin/env python3
"""
IPTV M3U Playlist Processor
Only includes live TV streams - filters out FM radio and media library completely.
"""

import re
import requests
import asyncio
import aiohttp
import sys
from typing import List, Dict, Any, Optional, Tuple, Set
from dataclasses import dataclass
import logging
import time

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
    'Connection': 'keep-alive',
}

# Keywords that indicate non-live content (case insensitive)
FILTER_KEYWORDS = [
    'radio', 'fm ', ' fm', 'music', 'audio', 'listen', 'station',
    'movie', 'series', 'vod', 'library', 'archive', 'catch-up', 'replay',
    'classical', 'jazz', 'rock', 'pop', 'hits', 'dance', 'electronic',
    'news radio', 'sports radio', 'talk radio', 'am ', ' am', '播', '广播',
    '影视', '电影', '电视剧', '综艺', '娱乐', '音乐', '电台'
]

# URL patterns that indicate non-live content
FILTER_URL_PATTERNS = [
    r'\.mp3$', r'\.aac$', r'\.ogg$', r'\.flac$', r'\.wav$',
    r'\.mp4$', r'\.mkv$', r'\.avi$', r'\.m4v$', r'\.mov$',
    r'/radio/', r'/music/', r'/audio/', r'/podcast/',
    r'/vod/', r'/movie/', r'/series/', r'/archive/',
    r'radio\.', r'music\.', r'audio\.'
]

@dataclass
class ChannelBlock:
    """Represents a complete channel block from M3U playlist"""
    lines: List[str]
    extinf_line: str
    url: str
    name: str
    tvg_id: str
    is_protected: bool = False
    is_live_tv: bool = True  # Will be set during filtering

class M3UParser:
    """Fast M3U parser with live TV filtering"""
    
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
                    block = self._create_block(current_block)
                    if self._is_live_tv(block):
                        blocks.append(block)
                    else:
                        logger.debug(f"Filtered out non-live: {block.name}")
                current_block = [line]
            elif current_block:
                current_block.append(line)
            
            i += 1
        
        # Add the last block
        if current_block:
            block = self._create_block(current_block)
            if self._is_live_tv(block):
                blocks.append(block)
            else:
                logger.debug(f"Filtered out non-live: {block.name}")
        
        self.channel_blocks = blocks
        logger.info(f"Kept {len(blocks)} live TV channels after filtering")
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
        
        # Extract name and tvg-id
        name = self._extract_name(extinf_line)
        tvg_id = self._extract_tvg_id(extinf_line)
        
        # Check for DRM/protection
        is_protected = any(
            'drm' in line.lower() or 
            'clearkey' in line.lower() or
            'token' in line.lower() or
            '?tk=' in line or
            'license' in line.lower() or
            '.mpd' in line.lower()
            for line in lines
        )
        
        return ChannelBlock(
            lines=lines,
            extinf_line=extinf_line,
            url=url,
            name=name,
            tvg_id=tvg_id,
            is_protected=is_protected
        )
    
    def _extract_name(self, extinf_line: str) -> str:
        """Extract channel name from EXTINF line"""
        if ',' in extinf_line:
            return extinf_line.split(',')[-1].strip()
        return "Unknown"
    
    def _extract_tvg_id(self, extinf_line: str) -> str:
        """Extract tvg-id from EXTINF line"""
        match = re.search(r'tvg-id="([^"]*)"', extinf_line)
        return match.group(1) if match else ""
    
    def _is_live_tv(self, block: ChannelBlock) -> bool:
        """Determine if a channel is live TV (not radio or media)"""
        
        # Combine all text for checking
        combined_text = f"{block.name} {block.url} {block.extinf_line}".lower()
        
        # Check for filter keywords
        for keyword in FILTER_KEYWORDS:
            if keyword.lower() in combined_text:
                return False
        
        # Check URL patterns
        for pattern in FILTER_URL_PATTERNS:
            if re.search(pattern, block.url.lower()):
                return False
        
        # If it has .m3u8 or .mpd, it's likely live TV
        if '.m3u8' in block.url.lower() or '.mpd' in block.url.lower():
            return True
        
        # Default to keeping it (better safe than sorry)
        return True
    
    def generate_playlist(self, blocks: List[ChannelBlock]) -> str:
        """Generate M3U playlist from channel blocks"""
        playlist = [self.header]
        
        for block in blocks:
            playlist.extend(block.lines)
        
        return '\n'.join(playlist)

class StreamValidator:
    """Fast stream validator for live TV only"""
    
    def __init__(self, timeout: int = 5, max_concurrent: int = 30):
        self.timeout = timeout
        self.max_concurrent = max_concurrent
        self.session = None
    
    async def check_stream(self, block: ChannelBlock) -> Tuple[bool, int]:
        """
        Check if a live TV stream is accessible
        Returns (keep_channel, status_code)
        """
        url = block.url
        if not url:
            return False, 0
        
        # Protected streams - keep them without checking
        if block.is_protected:
            logger.debug(f"Protected stream - keeping: {block.name}")
            return True, 0
        
        # Regular live streams - quick check
        try:
            async with aiohttp.ClientSession() as session:
                # Quick HEAD request
                async with session.head(url, timeout=self.timeout, 
                                       allow_redirects=True, 
                                       headers=DEFAULT_HEADERS) as response:
                    
                    # Consider 200 or redirects as working
                    if response.status == 200 or response.status in [301, 302, 307, 308]:
                        return True, response.status
                    else:
                        logger.debug(f"Bad status {response.status} for {block.name}")
                        return False, response.status
                        
        except asyncio.TimeoutError:
            logger.debug(f"Timeout for {block.name}")
            return False, 408
        except Exception as e:
            logger.debug(f"Error checking {block.name}: {e}")
            return False, 0
    
    async def validate_batch(self, blocks: List[ChannelBlock]) -> List[ChannelBlock]:
        """Validate multiple streams concurrently"""
        valid_blocks = []
        protected_count = 0
        working_count = 0
        dead_count = 0
        
        # Process in batches
        batch_size = 20
        total_batches = (len(blocks) + batch_size - 1) // batch_size
        
        for batch_num in range(total_batches):
            start = batch_num * batch_size
            end = min(start + batch_size, len(blocks))
            batch = blocks[start:end]
            
            logger.info(f"Checking batch {batch_num + 1}/{total_batches} ({len(batch)} channels)")
            
            tasks = []
            for block in batch:
                task = self.check_stream(block)
                tasks.append((block, task))
            
            # Wait for batch to complete
            for block, task in tasks:
                try:
                    keep, status = await task
                    
                    if keep:
                        valid_blocks.append(block)
                        if block.is_protected:
                            protected_count += 1
                        else:
                            working_count += 1
                            logger.info(f"✓ {block.name} - {status}")
                    else:
                        dead_count += 1
                        logger.warning(f"✗ DEAD: {block.name}")
                        
                except Exception as e:
                    logger.error(f"Error processing {block.name}: {e}")
                    # Keep if we can't verify (better safe than sorry)
                    valid_blocks.append(block)
                    if block.is_protected:
                        protected_count += 1
        
        logger.info(f"Results: {working_count} working, {dead_count} dead, {protected_count} protected kept")
        return valid_blocks

class PlaylistProcessor:
    """Main playlist processor - live TV only"""
    
    def __init__(self):
        self.parser = M3UParser()
        self.validator = StreamValidator()
    
    async def process(self, source_url: str) -> Optional[str]:
        """Main processing function"""
        try:
            start_time = time.time()
            
            # Download source playlist
            logger.info(f"Downloading playlist from {source_url}")
            content = self._download_playlist(source_url)
            if not content:
                logger.error("Failed to download playlist")
                return None
            
            # Parse and filter for live TV only
            logger.info("Parsing and filtering for live TV channels...")
            blocks = self.parser.parse(content)
            
            if not blocks:
                logger.warning("No live TV channels found in playlist")
                return None
            
            # Quick duplicate removal
            logger.info("Removing duplicates...")
            blocks = self._remove_duplicates(blocks)
            
            # Validate streams
            logger.info("Checking live TV streams...")
            valid_blocks = await self.validator.validate_batch(blocks)
            
            # Generate final playlist
            logger.info("Generating final playlist...")
            playlist = self.parser.generate_playlist(valid_blocks)
            
            elapsed = time.time() - start_time
            logger.info(f"Processing completed in {elapsed:.1f} seconds")
            
            return playlist
            
        except Exception as e:
            logger.error(f"Processing error: {e}")
            return None
    
    def _download_playlist(self, url: str) -> Optional[str]:
        """Download playlist"""
        try:
            response = requests.get(
                url, 
                timeout=10,
                headers=DEFAULT_HEADERS,
                allow_redirects=True
            )
            response.raise_for_status()
            return response.text
        except requests.RequestException as e:
            logger.warning(f"Download failed: {e}")
            return None
    
    def _remove_duplicates(self, blocks: List[ChannelBlock]) -> List[ChannelBlock]:
        """Remove duplicate channels based on URL"""
        seen_urls = set()
        unique_blocks = []
        
        for block in blocks:
            if block.url and block.url not in seen_urls:
                seen_urls.add(block.url)
                unique_blocks.append(block)
            elif not block.url:
                # Skip blocks without URLs
                pass
        
        removed = len(blocks) - len(unique_blocks)
        if removed > 0:
            logger.info(f"Removed {removed} duplicate channels")
        
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
        
        # Final stats
        lines = playlist.splitlines()
        channels = [l for l in lines if l.startswith('#EXTINF:')]
        
        logger.info("=" * 50)
        logger.info(f"✅ SUCCESS: Playlist saved to {output_file}")
        logger.info(f"📺 Live TV channels: {len(channels)}")
        logger.info(f"📊 Total lines: {len(lines)}")
        logger.info("=" * 50)
    else:
        logger.error("❌ Failed to process playlist")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
