#!/usr/bin/env python
import os
import sys
import requests
import json
import re
import time
import random
from typing import Tuple, Optional, Dict, List
from urllib.parse import unquote
from pathlib import Path
from dotenv import load_dotenv
import ssl
import urllib3
from requests.adapters import HTTPAdapter
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# SSL bypass
urllib3.disable_warnings()
ssl._create_default_https_context = ssl._create_unverified_context

load_dotenv()

class SSLAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        kwargs['ssl_context'] = ssl.create_default_context()
        kwargs['ssl_context'].check_hostname = False
        kwargs['ssl_context'].verify_mode = ssl.CERT_NONE
        return super().init_poolmanager(*args, **kwargs)

def setup_logger(name):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter('%(asctime)s - TikTok Downloader - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger

def create_directory(path):
    os.makedirs(path, exist_ok=True)

def sanitize_filename(filename):
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        filename = filename.replace(char, '_')
    return filename[:80]

class TikTokDownloader:
    
    def __init__(self):
        self.logger = setup_logger("TikTokDownloader")
        
        # Config
        self.proxy_username = os.getenv("DOWNLOAD_PROXY_USERNAME")
        self.proxy_password = os.getenv("DOWNLOAD_PROXY_PASSWORD")
        
        # Settings
        self.output_dir = "downloads"
        self.timeout = 45
        self.initial_concurrency = 5
        self.max_concurrency = 20
        self.min_concurrency = 2
        self.current_concurrency = self.initial_concurrency
        
        # Thread-safe stats
        self.stats_lock = threading.Lock()
        self.stats = {
            'successful': 0,
            'failed': 0, 
            'total': 0,
            'processed': 0,
            'sessions_created': 0,  # Track sessions created instead of "unique IPs"
            'concurrency_adjustments': 0,
            'peak_concurrency': self.initial_concurrency
        }
        
        # Performance tracking
        self.success_rate_window = []
        self.window_size = 20
        self.last_adjustment = time.time()
        self.adjustment_cooldown = 60
        
        # Actual IP tracking (will be populated from responses)
        self.actual_ips_seen = set()
        
        create_directory(self.output_dir)
        self.start_time = time.time()
        
        self.logger.info(f"TikTok Downloader initialized")
        self.logger.info(f"Dynamic IP allocation with adaptive concurrency")
        self.logger.info(f"Starting concurrency: {self.current_concurrency}")
    
    def _update_stats(self, key: str, value=1):
        
        with self.stats_lock:
            if isinstance(value, int) and key in self.stats:
                self.stats[key] += value
            else:
                self.stats[key] = value
    
    def _get_current_stats(self) -> Dict:
        """Get current stats safely"""
        with self.stats_lock:
            return self.stats.copy()
    
    def create_unique_session(self, video_index: int) -> Tuple[requests.Session, str]:
        """Create session with simple unique identifier"""
        session = requests.Session()
        session.mount('https://', SSLAdapter())
        session.mount('http://', SSLAdapter())
        session.verify = False
        
        # Simple session ID (don't track as "unique IP" until confirmed)
        session_id = f"video_{video_index}_{int(time.time())}"
        
        # Configure proxy
        proxy_url = f"http://{self.proxy_username}-session-{session_id}:{self.proxy_password}@brd.superproxy.io:33335"
        session.proxies.update({
            "http": proxy_url,
            "https": proxy_url
        })
        
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Cache-Control': 'no-cache',
        })
        
        # Track session creation
        self._update_stats('sessions_created')
        
        return session, session_id
    
    def _track_actual_ip(self, session: requests.Session):
        """Track actual IP address from response (optional)"""
        try:
            # Quick IP check (optional - adds overhead)
            response = session.get("https://httpbin.org/ip", timeout=10)
            if response.status_code == 200:
                ip_data = response.json()
                actual_ip = ip_data.get('origin', 'unknown')
                self.actual_ips_seen.add(actual_ip)
                return actual_ip
        except Exception:
            pass
        return None
    
    def extract_video_url_from_page(self, html_content: str, tiktok_url: str) -> Tuple[Optional[str], Dict]:
        """Extract video URL from TikTok page"""
        video_info = {}
        
        try:
            # Basic info
            uploader_match = re.search(r'/@([^/]+)/', tiktok_url)
            video_id_match = re.search(r'/video/(\d+)', tiktok_url)
            
            if uploader_match:
                video_info['uploader'] = uploader_match.group(1)
            if video_id_match:
                video_info['video_id'] = video_id_match.group(1)
            
            # Extract JSON
            json_start = html_content.find('<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"')
            if json_start == -1:
                return None, video_info
            
            json_start = html_content.find('>', json_start) + 1
            json_end = html_content.find('</script>', json_start)
            
            json_data = json.loads(html_content[json_start:json_end])
            item_struct = json_data['__DEFAULT_SCOPE__']['webapp.video-detail']['itemInfo']['itemStruct']
            
            # Update info
            author = item_struct.get('author', {})
            video_info.update({
                'uploader': author.get('uniqueId', video_info.get('uploader', 'unknown')),
                'description': item_struct.get('desc', '')
            })
            
            # Get video URL
            video_data = item_struct['video']
            
            download_addr = video_data.get('downloadAddr')
            if download_addr:
                return unquote(download_addr), video_info
            
            play_addr = video_data.get('playAddr')
            if play_addr:
                return unquote(play_addr), video_info
            
        except Exception as e:
            self.logger.debug(f"Extraction error: {str(e)}")
        
        return None, video_info
    
    def download_single_video(self, tiktok_url: str, video_index: int) -> Tuple[bool, str]:
        """Download single video with unique session"""
        
        try:
            # Create unique session
            session, session_id = self.create_unique_session(video_index)
            
            # Step 1: Get page
            response = session.get(tiktok_url, timeout=self.timeout)
            
            if response.status_code != 200:
                self._update_stats('failed')
                self._update_stats('processed')
                return False, f"Page failed: HTTP {response.status_code}"
            
            # Step 2: Extract
            video_url, video_info = self.extract_video_url_from_page(response.text, tiktok_url)
            
            if not video_url:
                self._update_stats('failed')
                self._update_stats('processed')
                return False, "No video URL found"
            
            # Step 3: Download with same session
            uploader = video_info.get('uploader', 'unknown')
            video_id = video_info.get('video_id', 'unknown')
            title = video_info.get('description', '')[:40]
            
            safe_title = sanitize_filename(title) if title else 'video'
            filename = f"{uploader}_{safe_title}_{video_id}.mp4"
            filepath = os.path.join(self.output_dir, filename)
            
            # Check exists
            if os.path.exists(filepath) and os.path.getsize(filepath) > 1000:
                self._update_stats('successful')
                self._update_stats('processed')
                return True, f"Already exists: {filename}"
            
            # Download
            session.headers.update({
                'Accept': '*/*',
                'Referer': tiktok_url,
                'Origin': 'https://www.tiktok.com',
            })
            
            video_response = session.get(video_url, stream=True, timeout=self.timeout)
            
            if video_response.status_code in [200, 206]:
                with open(filepath, 'wb') as f:
                    for chunk in video_response.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
                
                file_size = os.path.getsize(filepath)
                
                if file_size > 10000:
                    self._update_stats('successful')
                    self._update_stats('processed')
                    size_mb = file_size / (1024 * 1024)
                    return True, f"Success: {filename} ({size_mb:.1f}MB)"
                else:
                    os.remove(filepath)
                    self._update_stats('failed')
                    self._update_stats('processed')
                    return False, f"File too small: {file_size}B"
            else:
                self._update_stats('failed')
                self._update_stats('processed')
                return False, f"Download failed: HTTP {video_response.status_code}"
                
        except Exception as e:
            self._update_stats('failed')
            self._update_stats('processed')
            return False, f"Error: {str(e)}"
    
    def _log_progress(self, batch_completed: int, batch_total: int, batch_num: int):
        """Corrected progress logging"""
        current_stats = self._get_current_stats()
        elapsed = time.time() - self.start_time
        rate = current_stats['processed'] / elapsed * 60 if elapsed > 0 else 0
        success_rate = (current_stats['successful'] / max(current_stats['processed'], 1)) * 100
        
        self.logger.info(
            f"Progress: {current_stats['processed']}/{current_stats['total']} processed | "
            f"Success: {current_stats['successful']} ({success_rate:.1f}%) | "
            f"Rate: {rate:.1f} videos/min | "
            f"Batch {batch_num}: {batch_completed}/{batch_total} completed | "
            f"Concurrency: {self.current_concurrency} | "
            f"Sessions created: {current_stats['sessions_created']}"
        )
    
    def download_batch(self, urls: List[str], batch_num: int) -> List[Dict]:
        """Download batch with corrected logging"""
        results = []
        
        self.logger.info(f"Batch {batch_num}: Starting {len(urls)} URLs with concurrency {self.current_concurrency}")
        
        with ThreadPoolExecutor(max_workers=self.current_concurrency) as executor:
            future_to_data = {}
            for i, url in enumerate(urls):
                video_index = (batch_num - 1) * len(urls) + i
                future = executor.submit(self.download_single_video, url, video_index)
                future_to_data[future] = (url, video_index, i)
            
            completed = 0
            for future in as_completed(future_to_data):
                url, video_index, batch_index = future_to_data[future]
                
                try:
                    success, message = future.result(timeout=self.timeout + 30)
                    results.append({
                        'url': url,
                        'success': success,
                        'message': message
                    })
                    
                    self.success_rate_window.append(1 if success else 0)
                    completed += 1
                    
                    # Log progress every 5 completed
                    if completed % 5 == 0 or completed == len(urls):
                        self._log_progress(completed, len(urls), batch_num)
                    
                    time.sleep(random.uniform(0.5, 2.0))
                    
                except Exception as e:
                    results.append({
                        'url': url,
                        'success': False,
                        'message': f"Error: {str(e)}"
                    })
                    self._update_stats('failed')
                    self._update_stats('processed')
                    self.success_rate_window.append(0)
        
        return results
    
    def test_connection(self) -> bool:
        """Test connection"""
        try:
            session, session_id = self.create_unique_session(0)
            response = session.get("https://geo.brdtest.com/mygeo.json", timeout=30)
            
            if response.status_code == 200:
                geo_data = response.json()
                actual_ip = geo_data.get('ip_version')  # Get actual IP info
                country = geo_data.get('country')
                self.logger.info(f"Connection test successful - Country: {country}")
                return True
            else:
                return False
                
        except Exception as e:
            self.logger.error(f"Connection test error: {str(e)}")
            return False
    
    def _adaptive_concurrency_control(self):
        """Adjust concurrency based on performance"""
        current_time = time.time()
        
        if (len(self.success_rate_window) < self.window_size or 
            current_time - self.last_adjustment < self.adjustment_cooldown):
            return
        
        recent_success_rate = sum(self.success_rate_window) / len(self.success_rate_window)
        old_concurrency = self.current_concurrency
        
        if recent_success_rate > 0.90 and self.current_concurrency < self.max_concurrency:
            self.current_concurrency = min(self.current_concurrency + 2, self.max_concurrency)
            self.logger.info(f"Scaling up: Success rate {recent_success_rate:.1%} - concurrency {old_concurrency} to {self.current_concurrency}")
            
        elif recent_success_rate < 0.60 and self.current_concurrency > self.min_concurrency:
            self.current_concurrency = max(self.current_concurrency - 2, self.min_concurrency)
            self.logger.info(f"Scaling down: Success rate {recent_success_rate:.1%} - concurrency {old_concurrency} to {self.current_concurrency}")
        
        if old_concurrency != self.current_concurrency:
            self._update_stats('concurrency_adjustments')
            self._update_stats('peak_concurrency', max(self.stats['peak_concurrency'], self.current_concurrency))
            self.last_adjustment = current_time
        
        self.success_rate_window.clear()
    
    def download_all_videos(self, urls: List[str]) -> Dict:
        """Download all videos"""
        self.stats['total'] = len(urls)
        
        self.logger.info(f"TikTok Downloader starting: {len(urls)} videos")
        self.logger.info(f"Strategy: One video per session with dynamic IP allocation")
        
        results = []
        batch_size = 30
        
        for i in range(0, len(urls), batch_size):
            batch = urls[i:i + batch_size]
            batch_num = (i // batch_size) + 1
            total_batches = (len(urls) + batch_size - 1) // batch_size
            
            batch_start_time = time.time()
            batch_results = self.download_batch(batch, batch_num)
            batch_duration = time.time() - batch_start_time
            
            results.extend(batch_results)
            
            # Correct batch analysis
            batch_successful = sum(1 for r in batch_results if r['success'])
            batch_rate = len(batch) / batch_duration if batch_duration > 0 else 0
            batch_success_rate = (batch_successful / len(batch)) * 100
            
            # Get accurate overall stats
            current_stats = self._get_current_stats()
            overall_success_rate = (current_stats['successful'] / max(current_stats['processed'], 1)) * 100
            
            self.logger.info(
                f"Batch {batch_num}/{total_batches} completed: "
                f"{batch_successful}/{len(batch)} successful ({batch_success_rate:.1f}%) | "
                f"Rate: {batch_rate:.1f} videos/min | "
                f"Duration: {batch_duration:.1f}s"
            )
            
            self.logger.info(
                f"Overall progress: {current_stats['successful']}/{current_stats['processed']} successful ({overall_success_rate:.1f}%) | "
                f"Sessions created: {current_stats['sessions_created']}"
            )
            
            # Adaptive pause
            if batch_success_rate < 50:
                pause_time = random.uniform(20, 30)
                self.logger.info(f"Poor batch performance, extended pause: {pause_time:.1f}s")
            elif batch_success_rate > 90:
                pause_time = random.uniform(5, 10)
            else:
                pause_time = random.uniform(10, 15)
            
            if i + batch_size < len(urls):
                time.sleep(pause_time)
        
        return {'results': results, 'stats': self.stats}

def main():
    """Main function"""
    if len(sys.argv) != 2:
        print("Usage: python tiktok_downloader.py urls.txt")
        sys.exit(1)
    
    url_file = sys.argv[1]
    
    with open(url_file, 'r') as f:
        urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]
    
    print(f"TikTok Downloader")
    print(f"URLs to process: {len(urls)}")
    print(f"Dynamic IP allocation with adaptive scaling")
    
    downloader = TikTokDownloader()
    
    if not downloader.test_connection():
        print("Connection test failed")
        sys.exit(1)
    
    start_time = time.time()
    results = downloader.download_all_videos(urls)
    duration = time.time() - start_time
    
    stats = results['stats']
    print(f"\nTikTok Downloader completed")
    print(f"Results: {stats['successful']}/{stats['total']} successful ({(stats['successful']/stats['total']*100):.1f}%)")
    print(f"Duration: {duration/60:.1f} minutes")
    print(f"Rate: {(stats['total']/(duration/60)):.1f} videos per minute")
    print(f"Sessions created: {stats['sessions_created']}")
    print(f"Peak concurrency: {stats['peak_concurrency']}")

if __name__ == "__main__":
    main()
