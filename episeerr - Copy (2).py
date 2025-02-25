import os
import json
import time
import requests
import logging
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Create logs directory in the current working directory
log_dir = os.path.join(os.getcwd(), 'logs')
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, 'episeerr.log')

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    filename=log_file,
    filemode='a'
)

# Add console handler for direct output
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)
console_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
console_handler.setFormatter(console_formatter)
logging.getLogger().addHandler(console_handler)

logger = logging.getLogger(__name__)

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
    try:
        queue_response = requests.get(f"{SONARR_URL}/api/v3/queue", headers=headers)
        
        if not queue_response.ok:
            logger.error(f"Failed to retrieve queue. Status: {queue_response.status_code}")
            return 0
        
        queue = queue_response.json()
        
        series_queue_items = [
            item for item in queue.get('records', []) 
            if item.get('seriesId') == series_id and
               item.get('seasonNumber') == season_number and
               item.get('episodeNumber') not in episodes_to_keep
        ]
        
        if not series_queue_items:
            logger.info(f"No downloads to cancel for series ID {series_id}, season {season_number}")
            return 0
        
        remove_payload = {
            "ids": [item['id'] for item in series_queue_items],
            "removeFromClient": True,
            "removeFromDownloadClient": True
        }
        
        bulk_remove_response = requests.delete(
            f"{SONARR_URL}/api/v3/queue/bulk",
            headers=headers,
            json=remove_payload
        )
        
        if bulk_remove_response.ok:
            logger.info(f"Cancelled {len(series_queue_items)} downloads for series ID {series_id}, season {season_number}, keeping episodes {episodes_to_keep}")
            return len(series_queue_items)
        else:
            logger.error(f"Failed to cancel downloads. Status: {bulk_remove_response.status_code}")
            return 0
    
    except Exception as e:
        logger.error(f"Error cancelling series downloads: {str(e)}", exc_info=True)
        return 0
def wait_and_setup_series(tvdb_id, requested_season, max_attempts=12):
    """Wait for series to appear in Sonarr and set up monitoring only if ep## tags are present."""
    headers = get_sonarr_headers()
    
    for attempt in range(max_attempts):
        try:
            logger.info(f"Checking for series (attempt {attempt + 1}/{max_attempts})")
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
            
            logger.info(f"Will monitor episodes {episodes_to_monitor} of season {requested_season}")
            
            # Now that we know we have ep## tags, proceed with the workflow
            cancel_series_downloads(series_id, requested_season, episodes_to_monitor, headers)
            unmonitor_season(series_id, requested_season, headers)
            
            # Wait for Sonarr's database to update
            time.sleep(10)  # Adjust this value as needed
            
            monitor_specific_episodes(series_id, requested_season, episodes_to_monitor, headers)
            
            # Remove episode tag associations AFTER processing
            if not remove_episode_tags_from_series(series, headers):
                logger.error(f"Failed to remove episode tag associations from series {series['title']}")
                return False
            
            return True
        
        except Exception as e:
            logger.error(f"Error during setup: {str(e)}", exc_info=True)
            
        time.sleep(5)
    
    logger.error(f"Series not found after {max_attempts} attempts")
    return False

# Flask application
app = Flask(__name__)
@app.route('/sonarr_webhook', methods=['POST'])
def handle_sonarr_webhook():
    try:
        payload = request.json
        event_type = payload.get('eventType')
        
        if event_type == 'Grab':
            series = payload.get('series', {})
            episodes = payload.get('episodes', [])
            
            if series and episodes:
                series_id = series.get('id')
                tvdb_id = series.get('tvdbId')
                
                if series_id and tvdb_id:
                    headers = get_sonarr_headers()
                    full_series_info = get_series_by_id(series_id, headers)
                    
                    if full_series_info:
                        episodes_to_monitor = get_episode_numbers_from_tags(full_series_info, headers)
                        if episodes_to_monitor:
                            logger.info(f"Processing series {series['title']} based on Sonarr grab event")
                            process_series_with_tags(full_series_info, episodes_to_monitor)
        
        return jsonify({"message": "Processed Sonarr webhook"}), 200
    except Exception as e:
        logger.error(f"Error processing Sonarr webhook: {str(e)}")
        return jsonify({"error": "Processing failed"}), 500

def get_series_by_id(series_id, headers):
    response = requests.get(f"{SONARR_URL}/api/v3/series/{series_id}", headers=headers)
    if response.ok:
        return response.json()
    logger.error(f"Failed to get series info. Status: {response.status_code}")
    return None

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
            logger.error("No requested season found in payload")
            return jsonify({"error": "No season info"}), 400

        logger.info(f"Found requested season: {requested_season}")

        # Process any approved request for TV content
        if ('APPROVED' in payload.get('notification_type', '').upper() and 
            payload.get('media', {}).get('media_type') == 'tv'):
            
            tvdb_id = payload.get('media', {}).get('tvdbId')
            if not tvdb_id:
                return jsonify({"error": "No TVDB ID"}), 400
            
            # Wait for series and set up monitoring
            success = wait_and_setup_series(tvdb_id, requested_season)
            
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