# TikTok Downloader

A Python script for downloading TikTok videos at scale using Bright Data's proxy network.

## Features

- Downloads TikTok videos from a list of URLs
- Uses shared IPs for maximum scaling
- One video per IP session to maintain authentication context
- Adaptive concurrency based on success rates
- Automatic retry and error handling
- Progress tracking and detailed reporting

## Requirements

- Python 3.7+
- Bright Data account with a configured proxy zone, shared IPs. Residential proxies improve the success rate up to 100%
- TikTok video URLs as a csv file

## Installation

1. Install required packages:
```bash
pip install requests python-dotenv urllib3 pathlib
```

2. Create a .env file with your Bright Data credentials, the country code is two-character one and is optional:

```
DOWNLOAD_PROXY_USERNAME=brd-customer-hl_YOUR_CUSTOMER_ID-zone-YOUR_ZONE_NAME-country-us|de|...
DOWNLOAD_PROXY_PASSWORD=YOUR_ZONE_PASSWORD
```
3. Create a file with Tiktok URLs, one per line:

```
https://www.tiktok.com/@username/video/1234567890123456789
https://www.tiktok.com/@username/video/9876543210987654321
```

## Run
Simply run the script with the URLs file as a parameter:
```
python downloader.py urls.txt
```

## Output
The videos are saved in `downloads` directory
