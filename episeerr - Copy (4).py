import os
import json
import time
import requests
import logging
import threading
from logging.handlers import RotatingFileHandler
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Create logs directory in the current working directory
log_dir = os.path.join(os.getcwd(), 'logs')
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, 'episeerr.log')

# Create logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# Clear any existing handlers
logger.handlers.clear()

# Create rotating file handler
file_handler = RotatingFileHandler(
    log_file, 
    maxBytes=10*1024*1024,  # 10 MB
    backupCount=1,  # Keep one backup file
    encoding='utf-8'
)
file_handler.setLevel(logging.DEBUG)
file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(file_formatter)

# Create console handler
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)
console_handler.setFormatter(file_formatter)

# Add handlers to logger
logger.addHandler(file_handler)
logger.addHandler(console_handler)

# Sonarr connection details
SONARR_URL = os.getenv('SONARR_URL', 'http://sonarr:8989')
SONARR_API_KEY = os.getenv('SONARR_API_KEY')

def get_sonarr_headers():
    """Get headers for Sonarr API requests."""
    return {
        'X-Api-Key': SONARR_API_KEY,
        'Content-Type': 'application/json'
    }

def create_episode_tags():
    """Create episode tags from ep01 to ep50 in Sonarr."""
    try:
        headers = get_sonarr_headers()
        logger.debug(f"Making request to {SONARR_URL}/api/v3/tag")
        
        # Get existing tags
        tags_response = requests.get(
            f"{SONARR_URL}/api/v3/tag",
            headers=headers
        )
        
        if not tags_response.ok:
            logger.error(f"Failed to get tags. Status: {tags_response.status_code}")
            return []

        existing_tags = {tag['label'].lower() for tag in tags_response.json()}
        
        # Create missing tags
        created_tags = []
        for i in range(1, 51):
            tag_name = f"ep{i:02d}"
            if tag_name.lower() not in existing_tags:
                tag_create_response = requests.post(
                    f"{SONARR_URL}/api/v3/tag",
                    headers=headers,
                    json={"label": tag_name}
                )
                if tag_create_response.ok:
                    created_tags.append(tag_name)
                    logger.info(f"Created tag: {tag_name}")
        
        return created_tags
    except Exception as e:
        logger.error(f"Error creating episode tags: {str(e)}")
        return []

def get_episode_numbers_from_tags(series, headers):
    """Get all episode numbers to monitor from series tags."""
    try:
        # If series has no tags, return empty list
        if not series.get('tags'):
            return []

        episode_numbers = []
        # For each tag, fetch its details
        for tag_id in series['tags']:
            tag_response = requests.get(
                f"{SONARR_URL}/api/v3/tag/{tag_id}",
                headers=headers
            )
            
            if tag_response.ok:
                tag_details = tag_response.json()
                
                # Check if tag label starts with 'ep'
                if tag_details['label'].startswith('ep'):
                    episode_number = int(tag_details['label'][2:])
                    episode_numbers.append(episode_number)
                    logger.info(f"Found episode tag: {tag_details['label']}")
        
        # Sort the episode numbers for consistency
        return sorted(episode_numbers)
    
    except Exception as e:
        logger.error(f"Error getting episode tags: {str(e)}", exc_info=True)
    
    logger.info("No episode tags found")
    return []

def unmonitor_season(series_id, season_number, headers):
    episodes_response = requests.get(
        f"{SONARR_URL}/api/v3/episode?seriesId={series_id}",
        headers=headers
    )
    
    if not episodes_response.ok:
        logger.error(f"Failed to get episodes. Status: {episodes_response.status_code}")
        return

    episodes = episodes_response.json()
    season_episode_ids = [
        ep['id'] for ep in episodes 
        if ep['seasonNumber'] == season_number
    ]
    
    if season_episode_ids:
        unmonitor_response = requests.put(
            f"{SONARR_URL}/api/v3/episode/monitor",
            headers=headers,
            json={"episodeIds": season_episode_ids, "monitored": False}
        )
        if not unmonitor_response.ok:
            logger.error(f"Failed to unmonitor episodes. Status: {unmonitor_response.status_code}")
        else:
            logger.info(f"Unmonitored all episodes in season {season_number}")

def monitor_specific_episodes(series_id, season_number, episode_numbers, headers):
    episodes_response = requests.get(
        f"{SONARR_URL}/api/v3/episode?seriesId={series_id}",
        headers=headers
    )
    
    if not episodes_response.ok:
        logger.error(f"Failed to get episodes. Status: {episodes_response.status_code}")
        return

    episodes = episodes_response.json()
    target_episodes = [
        ep for ep in episodes 
        if ep['seasonNumber'] == season_number and 
        ep['episodeNumber'] in episode_numbers
    ]
    
    if not target_episodes:
        logger.error(f"Target episodes {episode_numbers} not found in season {season_number}")
        return
    
    monitor_episode_ids = [ep['id'] for ep in target_episodes]
    monitor_response = requests.put(
        f"{SONARR_URL}/api/v3/episode/monitor",
        headers=headers,
        json={"episodeIds": monitor_episode_ids, "monitored": True}
    )
    
    if not monitor_response.ok:
        logger.error(f"Failed to monitor episodes. Status: {monitor_response.status_code}")
    else:
        logger.info(f"Monitoring episodes {episode_numbers} in season {season_number}")

def remove_episode_tags_from_series(series, headers):
    """Remove only 'ep##' tag associations from a series."""
    series_id = series['id']
    current_tag_ids = series.get('tags', [])
    
    # Get details for the current tags of the series
    episode_tag_ids = []
    for tag_id in current_tag_ids:
        tag_response = requests.get(f"{SONARR_URL}/api/v3/tag/{tag_id}", headers=headers)
        if tag_response.ok:
            tag_details = tag_response.json()
            if tag_details['label'].startswith('ep'):
                episode_tag_ids.append(tag_id)
    
    if not episode_tag_ids:
        logger.info(f"No episode tags found for series {series['title']}")
        return True
    
    # Remove episode tag associations from series
    updated_tag_ids = [tag_id for tag_id in current_tag_ids if tag_id not in episode_tag_ids]
    series_update = series.copy()
    series_update['tags'] = updated_tag_ids
    
    update_response = requests.put(
        f"{SONARR_URL}/api/v3/series/{series_id}",
        headers=headers,
        json=series_update
    )
    
    if not update_response.ok:
        logger.error(f"Failed to update series tags. Status: {update_response.status_code}")
        return False
    
    logger.info(f"Removed episode tag associations from series {series['title']}")
    return True

def cancel_series_downloads(series_id, season_number, episodes_to_keep, headers):
    """
    Cancel downloads for a specific series and season, except for specified episodes.
    """
    try:
        # Extensive logging to understand the parameters
        logger.debug(f"Cancel Downloads - Input Parameters:")
        logger.debug(f"Series ID: {series_id}")
        logger.debug(f"Season Number: {season_number}")
        logger.debug(f"Episodes to Keep: {episodes_to_keep}")
        logger.debug(f"Episodes to Keep Type: {type(episodes_to_keep)}")
        logger.debug(f"Episodes to Keep Length: {len(episodes_to_keep) if episodes_to_keep else 'N/A'}")
        
        # Ensure episodes_to_keep is a list
        if not episodes_to_keep:
            logger.warning("No episodes specified to keep. Will cancel ALL downloads.")
            episodes_to_keep = []
        
        # Get check parameters from environment
        check_interval = int(os.getenv('SONARR_QUEUE_CHECK_INTERVAL', 15))  # Default 15 seconds
        max_checks = int(os.getenv('SONARR_QUEUE_MAX_CHECKS', 8))  # Default 8 checks
        
        total_cancelled = 0
        
        for check_num in range(max_checks):
            # Check queue
            queue_response = requests.get(f"{SONARR_URL}/api/v3/queue", headers=headers)
            if not queue_response.ok:
                logger.error(f"Failed to retrieve queue. Status: {queue_response.status_code}")
                return 0
            
            queue = queue_response.json()
            logger.debug(f"Full queue: {json.dumps(queue, indent=2)}")
            
            # More detailed queue item logging
            logger.debug("Queue Items Details:")
            for item in queue.get('records', []):
                logger.debug(f"Queue Item - Series ID: {item.get('seriesId')}, "
                             f"Season: {item.get('seasonNumber')}, "
                             f"Episode: {item.get('episodeNumber')}")
            
            # Filter for items to cancel
            queue_items_to_cancel = [
                item for item in queue.get('records', []) 
                if (item.get('seriesId') == series_id and
                    (season_number is None or item.get('seasonNumber') == season_number) and
                    str(item.get('episodeNumber')) not in map(str, episodes_to_keep))
            ]
            
            logger.debug(f"Matching Queue Items to Cancel: {len(queue_items_to_cancel)}")
            
            if queue_items_to_cancel:
                # Detailed logging of items to be cancelled
                for item in queue_items_to_cancel:
                    logger.info(f"Cancelling download - Series ID: {item.get('seriesId')}, "
                                f"Season: {item.get('seasonNumber')}, "
                                f"Episode: {item.get('episodeNumber')}")
                
                remove_payload = {
                    "ids": [item['id'] for item in queue_items_to_cancel],
                    "removeFromClient": True,
                    "removeFromDownloadClient": True
                }
                
                bulk_remove_response = requests.delete(
                    f"{SONARR_URL}/api/v3/queue/bulk",
                    headers=headers,
                    json=remove_payload
                )
                
                if bulk_remove_response.ok:
                    total_cancelled += len(queue_items_to_cancel)
                    logger.info(f"Cancelled {len(queue_items_to_cancel)} downloads for series ID {series_id}, season {season_number}")
                else:
                    logger.error(f"Failed to cancel downloads. Status: {bulk_remove_response.status_code}")
                    logger.error(f"Response content: {bulk_remove_response.text}")
                    return 0
            
            # Wait between checks (skip after last check)
            if check_num < max_checks - 1:
                logger.info(f"Waiting {check_interval} seconds before next check...")
                time.sleep(check_interval)
        
        return total_cancelled
    
    except Exception as e:
        logger.error(f"Error cancelling series downloads: {str(e)}", exc_info=True)
        return 0
    
def get_overseerr_headers():
    """Get headers for Overseerr API requests."""
    return {
        'X-Api-Key': os.getenv('OVERSEERR_API_KEY'),
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }

def find_overseerr_request(tvdb_id, season):
    """
    Find the specific request for a TV series and season.
    
    :param tvdb_id: TVDB ID of the series
    :param season: Season number
    :return: Request ID if found, None otherwise
    """
    try:
        overseerr_url = os.getenv('OVERSEERR_URL')
        headers = get_overseerr_headers()
        
        # Log the full API call details for debugging
        logger.debug(f"Overseerr API URL: {overseerr_url}")
        logger.debug(f"Overseerr API Headers: {headers}")
        
        # Get all pending requests
        requests_response = requests.get(
            f"{overseerr_url}/api/v1/request",
            headers=headers,
            params={
                'filter': 'pending',
                'take': 100  # Adjust as needed
            }
        )
        
        # Log full response for debugging
        logger.debug(f"Overseerr Response Status: {requests_response.status_code}")
        logger.debug(f"Overseerr Response Headers: {requests_response.headers}")
        try:
            response_json = requests_response.json()
            logger.debug(f"Overseerr Response JSON: {json.dumps(response_json, indent=2)}")
        except ValueError:
            logger.debug(f"Overseerr Response Text: {requests_response.text}")
        
        if not requests_response.ok:
            logger.error(f"Failed to get Overseerr requests. Status: {requests_response.status_code}")
            return None
        
        requests_data = requests_response.json()
        
        # Find matching request
        for request in requests_data.get('results', []):
            media = request.get('media', {})
            if (media.get('tvdbId') == int(tvdb_id) and 
                media.get('mediaType') == 'TV' and 
                any(s.get('seasonNumber') == season for s in request.get('seasons', []))):
                return request.get('id')
        
        logger.warning(f"No matching request found for TVDB ID {tvdb_id}, season {season}")
        return None
    
    except Exception as e:
        logger.error(f"Error finding Overseerr request: {str(e)}", exc_info=True)
        return None

def delete_overseerr_request(request_id):
    """
    Delete a specific request in Overseerr.
    
    :param request_id: ID of the request to delete
    :return: True if successful, False otherwise
    """
    try:
        overseerr_url = os.getenv('OVERSEERR_URL')
        headers = get_overseerr_headers()
        
        # Log the deletion attempt
        logger.info(f"Attempting to delete Jellyseerr request {request_id}")
        
        delete_response = requests.delete(
            f"{overseerr_url}/api/v1/request/{request_id}",
            headers=headers
        )
        
        # Log full response for debugging
        logger.debug(f"Delete Request Response Status: {delete_response.status_code}")
        try:
            response_json = delete_response.json()
            logger.debug(f"Delete Request Response JSON: {json.dumps(response_json, indent=2)}")
        except ValueError:
            logger.debug(f"Delete Request Response Text: {delete_response.text}")
        
        if delete_response.ok or delete_response.status_code == 404:
            # 404 means the request was already deleted
            logger.info(f"Successfully deleted or request not found: {request_id}")
            return True
        else:
            logger.error(f"Failed to delete request {request_id}. Status: {delete_response.status_code}")
            return False
    
    except Exception as e:
        logger.error(f"Error deleting Jellyseerr request: {str(e)}", exc_info=True)
        return False
        

def log_missing_episodes(tvdb_id, season, requested_episodes, found_episodes):
    """
    Log missing episodes to a JSON file for potential future reference.
    
    :param tvdb_id: TVDB ID of the series
    :param season: Season number
    :param requested_episodes: List of episodes initially requested
    :param found_episodes: List of episodes actually found
    """
    log_dir = os.path.join(os.getcwd(), 'logs', 'missing_episodes')
    os.makedirs(log_dir, exist_ok=True)
    
    log_file = os.path.join(log_dir, f'{tvdb_id}_season_{season}_missing.json')
    
    missing_data = {
        'tvdb_id': tvdb_id,
        'season': season,
        'requested_episodes': requested_episodes,
        'found_episodes': found_episodes,
        'missing_episodes': [ep for ep in requested_episodes if ep not in found_episodes],
        'timestamp': time.time()
    }
    
    try:
        with open(log_file, 'w') as f:
            json.dump(missing_data, f, indent=2)
        
        logger.info(f"Logged missing episodes for TVDB ID {tvdb_id}, season {season}")
    except Exception as e:
        logger.error(f"Error logging missing episodes: {str(e)}")

# Global variable to store recent request information
recent_requests = {}
def get_series_by_id(series_id, headers):
    """
    Retrieve full series information by Sonarr series ID.
    
    :param series_id: ID of the series in Sonarr
    :param headers: API headers for Sonarr
    :return: Series information or None
    """
    try:
        response = requests.get(f"{SONARR_URL}/api/v3/series/{series_id}", headers=headers)
        if response.ok:
            return response.json()
        logger.error(f"Failed to get series info. Status: {response.status_code}")
        return None
    except Exception as e:
        logger.error(f"Error retrieving series by ID: {str(e)}")
        return None

def remove_all_unmonitored_episodes(series_id, season_number, headers):
    """
    Ensure no unmonitored episodes can be downloaded.
    
    :param series_id: ID of the series
    :param season_number: Season number to focus on
    :param headers: API headers for Sonarr
    """
    try:
        # Get all episodes for the series
        episodes_response = requests.get(
            f"{SONARR_URL}/api/v3/episode?seriesId={series_id}", 
            headers=headers
        )
        
        if not episodes_response.ok:
            logger.error(f"Failed to retrieve episodes for series ID {series_id}")
            return
        
        episodes = episodes_response.json()
        
        # Find episodes in the specified season that should be unmonitored
        episodes_to_unmonitor = [
            ep['id'] for ep in episodes 
            if ep['seasonNumber'] == season_number
        ]
        
        if not episodes_to_unmonitor:
            logger.warning(f"No episodes found to unmonitor for series ID {series_id}, season {season_number}")
            return
        
        # Unmonitor these episodes
        unmonitor_response = requests.put(
            f"{SONARR_URL}/api/v3/episode/monitor",
            headers=headers,
            json={
                "episodeIds": episodes_to_unmonitor,
                "monitored": False
            }
        )
        
        if unmonitor_response.ok:
            logger.info(f"Unmonitored all episodes in season {season_number}")
        else:
            logger.error(f"Failed to unmonitor episodes. Status: {unmonitor_response.status_code}")
    
    except Exception as e:
        logger.error(f"Error unmonitoring episodes: {str(e)}", exc_info=True)

def log_series_filter(tvdb_id, series_id, season, episodes_to_monitor):
    """
    Log series filter information to a JSON file
    
    :param tvdb_id: TVDB ID of the series
    :param series_id: Sonarr series ID
    :param season: Season number
    :param episodes_to_monitor: List of episodes to monitor
    """
    try:
        # Create logs directory if it doesn't exist
        log_dir = os.path.join(os.getcwd(), 'logs', 'series_filters')
        os.makedirs(log_dir, exist_ok=True)
        
        # Create filename based on TVDB ID
        log_file = os.path.join(log_dir, f'{tvdb_id}_filter.json')
        
        filter_data = {
            'tvdb_id': tvdb_id,
            'series_id': series_id,
            'season': season,
            'episodes_to_monitor': episodes_to_monitor,
            'timestamp': time.time()
        }
        
        with open(log_file, 'w') as f:
            json.dump(filter_data, f, indent=2)
        
        logger.info(f"Logged filter information for TVDB ID {tvdb_id}")
    except Exception as e:
        logger.error(f"Error logging filter information: {str(e)}")

def read_series_filter(tvdb_id):
    """
    Read series filter information from JSON file
    
    :param tvdb_id: TVDB ID of the series
    :return: Filter information or None
    """
    try:
        log_dir = os.path.join(os.getcwd(), 'logs', 'series_filters')
        log_file = os.path.join(log_dir, f'{tvdb_id}_filter.json')
        
        if not os.path.exists(log_file):
            return None
        
        with open(log_file, 'r') as f:
            filter_data = json.load(f)
        
        # Remove the file after reading
        os.remove(log_file)
        
        return filter_data
    except Exception as e:
        logger.error(f"Error reading filter information: {str(e)}")
        return None

def cleanup_series_filter():
    """
    Background thread to cleanup series that haven't been fully processed
    """
    while True:
        # Wait for a short period (e.g., 5 minutes)
        time.sleep(300)  # 5 minutes
        
        logger.info("Starting background cleanup of unprocessed series")
        
        # Get logs directory
        log_dir = os.path.join(os.getcwd(), 'logs', 'series_filters')
        
        # Check for any remaining filter files
        try:
            filter_files = [f for f in os.listdir(log_dir) if f.endswith('_filter.json')]
            
            for filter_file in filter_files:
                tvdb_id = filter_file.replace('_filter.json', '')
                
                try:
                    # Read the filter information
                    with open(os.path.join(log_dir, filter_file), 'r') as f:
                        filter_data = json.load(f)
                    
                    # Verify the filter is old enough to cleanup
                    if time.time() - filter_data.get('timestamp', 0) > 300:  # 5 minutes
                        headers = get_sonarr_headers()
                        
                        # Cancel downloads
                        queue_response = requests.get(f"{SONARR_URL}/api/v3/queue", headers=headers)
                        if queue_response.ok:
                            queue = queue_response.json()
                            
                            queue_items_to_remove = [
                                item['id'] for item in queue.get('records', [])
                                if item.get('seriesId') == filter_data['series_id'] and 
                                   item.get('seasonNumber') == filter_data['season']
                            ]
                            
                            if queue_items_to_remove:
                                remove_payload = {
                                    "ids": queue_items_to_remove,
                                    "removeFromClient": True,
                                    "removeFromDownloadClient": True
                                }
                                
                                bulk_remove_response = requests.delete(
                                    f"{SONARR_URL}/api/v3/queue/bulk",
                                    headers=headers,
                                    json=remove_payload
                                )
                                
                                if bulk_remove_response.ok:
                                    logger.info(f"Cleanup: Cancelled {len(queue_items_to_remove)} downloads for series ID {filter_data['series_id']}, season {filter_data['season']}")
                        
                        # Remove the file
                        os.remove(os.path.join(log_dir, filter_file))
                
                except Exception as e:
                    logger.error(f"Cleanup error for filter file {filter_file}: {str(e)}")
        
        except Exception as e:
            logger.error(f"Error during series filter cleanup: {str(e)}")

def start_cleanup_thread():
    """Start the cleanup thread as a daemon"""
    cleanup_thread = threading.Thread(target=cleanup_series_filter, daemon=True)
    cleanup_thread.start()

def wait_and_setup_series(tvdb_id, requested_season, request_id=None):
    """
    Wait for series to appear in Sonarr and set up monitoring only if ep## tags are present.
    
    :param tvdb_id: TVDB ID of the series
    :param requested_season: Season to monitor
    :param request_id: Optional Jellyseerr request ID to delete later
    """
    headers = get_sonarr_headers()
    
    for attempt in range(12):  # Max attempts unchanged
        try:
            logger.info(f"Checking for series (attempt {attempt + 1}/12)")
            response = requests.get(f"{SONARR_URL}/api/v3/series", headers=headers)
            
            if not response.ok:
                logger.error(f"Failed to get series list. Status: {response.status_code}")
                continue

            series_list = response.json()
            matching_series = [s for s in series_list if str(s.get('tvdbId')) == str(tvdb_id)]
            
            if not matching_series:
                logger.warning(f"No matching series found for TVDB ID {tvdb_id}")
                time.sleep(5)
                continue

            series = matching_series[0]
            series_id = series['id']
            logger.info(f"Found series: {series['title']} (ID: {series_id})")
            
            # First check for ep## tags
            episodes_to_monitor = get_episode_numbers_from_tags(series, headers)
            
            if not episodes_to_monitor:
                logger.info(f"No ep## tags found initially for series {series['title']}. Waiting and checking again.")
                time.sleep(10)  # Wait 10 seconds before checking again
                episodes_to_monitor = get_episode_numbers_from_tags(series, headers)
            
            if not episodes_to_monitor:
                logger.info(f"No ep## tags found after second check for series {series['title']}. Taking no action.")
                return True  # Return True as this is the expected behavior for series without ep## tags
            
            logger.info(f"Will attempt to monitor episodes {episodes_to_monitor} of season {requested_season}")
            
            # Get episodes for the specific season
            episodes_response = requests.get(
                f"{SONARR_URL}/api/v3/episode?seriesId={series_id}&seasonNumber={requested_season}", 
                headers=headers
            )
            
            if not episodes_response.ok:
                logger.error(f"Failed to retrieve episodes for series {series['title']}")
                return False
            
            all_season_episodes = episodes_response.json()
            
            # Always continue processing, logging mismatches
            found_episodes = [
                ep['episodeNumber'] for ep in all_season_episodes 
                if ep['episodeNumber'] in episodes_to_monitor
            ]
            
            # Log missing episodes
            log_missing_episodes(tvdb_id, requested_season, episodes_to_monitor, found_episodes)
            
            logger.info(f"Found episodes: {found_episodes}")
            logger.warning(f"Missing episodes: {[ep for ep in episodes_to_monitor if ep not in found_episodes]}")

            recent_requests[str(tvdb_id)] = {
                'season': requested_season,
                'episodes_to_monitor': episodes_to_monitor,
                'timestamp': time.time()
            }

            #log_series_filter(str(tvdb_id), series_id, requested_season, episodes_to_monitor)
            # Monitor only the found episodes
            monitor_specific_episodes(series_id, requested_season, found_episodes, headers)
            # Unmonitor ALL episodes in the season first
            remove_all_unmonitored_episodes(series_id, requested_season, headers)

            # Monitor only the found episodes
            monitor_specific_episodes(series_id, requested_season, found_episodes, headers)
            
            # Cancel downloads for the found episodes
            cancel_series_downloads(series_id, requested_season, found_episodes, headers)
            
            # Wait for Sonarr's database to update
            time.sleep(10)  # Adjust this value as needed
            
            
            # Remove the episode tags from the series
            remove_episode_tags_from_series(series, headers)
            # Always return True to continue processing
            return True
        
        except Exception as e:
            logger.error(f"Error during setup: {str(e)}", exc_info=True)
            
        time.sleep(5)
    
    logger.error(f"Series not found after 12 attempts")
    return False
# Flask application
app = Flask(__name__)

    
@app.route('/webhook', methods=['POST'])
def handle_webhook():
    """Handle incoming webhooks."""
    logger.info("Received webhook request")
    logger.debug(f"Headers: {dict(request.headers)}")
    
    try:
        payload = request.json
        logger.debug(f"Received payload: {json.dumps(payload, indent=2)}")

        # Extract requested season from extra data
        requested_season = None
        for extra in payload.get('extra', []):
            if extra.get('name') == 'Requested Seasons':
                requested_season = int(extra.get('value'))
                break

        if not requested_season:
            logger.error("No season info found in payload")
            return jsonify({"error": "No season info"}), 400

        logger.info(f"Found requested season: {requested_season}")

        # Process any approved request for TV content
        if ('APPROVED' in payload.get('notification_type', '').upper() and 
            payload.get('media', {}).get('media_type') == 'tv'):
            
            tvdb_id = payload.get('media', {}).get('tvdbId')
            if not tvdb_id:
                return jsonify({"error": "No TVDB ID"}), 400
            
            # Extract request ID
            request_id = payload.get('request', {}).get('request_id')
            
            # Wait for series and set up monitoring
            success = wait_and_setup_series(tvdb_id, requested_season)
            
            # Always attempt to delete the request
            if request_id:
                try:
                    delete_success = delete_overseerr_request(request_id)
                    if not delete_success:
                        logger.error(f"Failed to delete request {request_id}")
                except Exception as delete_error:
                    logger.error(f"Exception during request deletion: {delete_error}")
            
            response = {
                "status": "success" if success else "failed",
                "message": "Set up monitoring" if success else "Failed to set up monitoring"
            }
            return jsonify(response), 200 if success else 500

        logger.info("Event ignored - not an approved TV request")
        return jsonify({"message": "Ignored event"}), 200
        
    except Exception as e:
        logger.error(f"Webhook processing error: {str(e)}", exc_info=True)
        return jsonify({"error": "Processing failed"}), 500
    
@app.route('/sonarr_webhook', methods=['POST'])
def handle_sonarr_webhook():
    """Handle Sonarr webhook for potential download filtering."""
    logger.info("Received Sonarr webhook request")
    logger.debug(f"Headers: {dict(request.headers)}")
   
    try:
        payload = request.json
        logger.debug(f"Received payload: {json.dumps(payload, indent=2)}")
       
        # Only process if it's a Grab event
        if payload.get('eventType') != 'Grab':
            logger.info("Event ignored - not a Sonarr grab event")
            return jsonify({"message": "Ignored event"}), 200
            
        series = payload.get('series', {})
        tvdb_id = series.get('tvdbId')
        series_id = series.get('id')
        
        # Get the episodes from the payload
        episodes = payload.get('episodes', [])
        if not episodes:
            logger.warning("No episodes found in payload")
            return jsonify({"message": "No episodes in payload"}), 200
       
        # Check if we have a recent request for this TVDB ID
        recent_request = recent_requests.get(str(tvdb_id))
       
        if not recent_request:
            logger.warning(f"No recent request found for TVDB ID {tvdb_id}")
            return jsonify({"message": "No recent request found"}), 200
            
        # Check if the request is recent (within last hour)
        if time.time() - recent_request.get('timestamp', 0) > 3600:  # 1 hour
            logger.warning(f"Recent request for TVDB ID {tvdb_id} is too old")
            return jsonify({"message": "Recent request is too old"}), 200

        # Return a 202 Accepted immediately, but continue processing after a delay
        response_thread = threading.Thread(
            target=process_grab_event_with_delay,
            args=(payload, tvdb_id, series_id, recent_request),
            daemon=True
        )
        response_thread.start()
        
        return jsonify({"message": "Processing grab event in background"}), 202
        
    except Exception as e:
        logger.error(f"Sonarr webhook processing error: {str(e)}", exc_info=True)
        return jsonify({"error": "Processing failed"}), 500

def process_grab_event_with_delay(payload, tvdb_id, series_id, recent_request):
    """Process grab event after an initial delay to allow downloads to appear in queue."""
    try:
        # Wait for downloads to start appearing in Sonarr's queue
        logger.info(f"Waiting 30 seconds for downloads to initialize before processing grab event...")
        time.sleep(45)  # Initial delay
        
        series = payload.get('series', {})
        episodes = payload.get('episodes', [])
        episodes_to_monitor = recent_request.get('episodes_to_monitor', [])
        grabbed_season = recent_request.get('season')
        
        # Get episodes from the grab event
        grabbed_episodes = []
        for ep in episodes:
            if ep.get('seasonNumber') == grabbed_season:
                grabbed_episodes.append(ep.get('episodeNumber'))
        
        # Check if any of the grabbed episodes are in our monitor list
        filtered_episodes = [
            ep for ep in episodes_to_monitor 
            if ep in grabbed_episodes
        ]
        
        if not filtered_episodes:
            logger.info(f"All grabbed episodes should be cancelled for series {series.get('title')}")
            filtered_episodes = episodes_to_monitor  # Keep only our monitored episodes
        else:
            logger.info(f"Some grabbed episodes match our filter - keeping episodes {filtered_episodes}")
            
        # Get Sonarr headers
        headers = get_sonarr_headers()
        
        # Unmonitor ALL episodes in the season first
        remove_all_unmonitored_episodes(series_id, grabbed_season, headers)
        
        # Cancel downloads for the found episodes (keep only the filtered episodes)
        cancel_series_downloads(series_id, grabbed_season, filtered_episodes, headers)
        
        # Monitor only the filtered episodes
        monitor_specific_episodes(series_id, grabbed_season, filtered_episodes, headers)
        
        # Update timestamp
        recent_requests[str(tvdb_id)]['timestamp'] = time.time()
        
        logger.info(f"Completed background processing of grab event for series {series.get('title')}")
        
    except Exception as e:
        logger.error(f"Error in background grab processing: {str(e)}", exc_info=True)
    
def main():
    # Log startup
    logger.info("EpisEERR Webhook Listener Starting")
    logger.info(f"Sonarr URL: {SONARR_URL}")

    # Create tags on startup
    created_tags = create_episode_tags()
    logger.info(f"Created or verified tags: {', '.join(created_tags) if created_tags else 'None'}")

    # Start webhook listener
    logger.info("Starting webhook listener on port 5000")
    app.run(host='0.0.0.0', port=5000)

if __name__ == '__main__':
    main()