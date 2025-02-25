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

def get_episode_number_from_tag(series, headers):
    """Get the episode number to monitor from series tag."""
    try:
        # If series has no tags, return None
        if not series.get('tags'):
            return None

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
                    logger.info(f"Found episode tag: {tag_details['label']}")
                    return episode_number
    
    except Exception as e:
        logger.error(f"Error getting episode tag: {str(e)}", exc_info=True)
    
    logger.info("No episode tag found")
    return None

def cancel_all_queue_items(series_id, headers):
    """Cancel all queued downloads for a series."""
    try:
        queue_response = requests.get(f"{SONARR_URL}/api/v3/queue", headers=headers)
        if queue_response.ok:
            queue = queue_response.json()
            
            # Cancel each queue item for this series
            for item in queue.get('records', []):
                if item.get('seriesId') == series_id:
                    cancel_response = requests.delete(
                        f"{SONARR_URL}/api/v3/queue/{item['id']}",
                        headers=headers,
                        params={'removeFromClient': 'true', 'blocklist': False}
                    )
                    if cancel_response.ok:
                        logger.info(f"Cancelled download: S{item['episode'].get('seasonNumber', '?')}E{item['episode'].get('episodeNumber', '?')}")
                    else:
                        logger.error(f"Failed to cancel download for item {item['id']}")
    except Exception as e:
        logger.error(f"Error cancelling queue items: {str(e)}")

def wait_and_setup_series(tvdb_id, requested_season, max_attempts=12):
    """Wait for series to appear in Sonarr and set up monitoring."""
    headers = get_sonarr_headers()
    
    for attempt in range(max_attempts):
        try:
            logger.info(f"Checking for series (attempt {attempt + 1}/{max_attempts})")
            response = requests.get(f"{SONARR_URL}/api/v3/series", headers=headers)
            
            if response.ok:
                series_list = response.json()
                matching_series = [s for s in series_list if str(s.get('tvdbId')) == str(tvdb_id)]
                
                if matching_series:
                    series = matching_series[0]
                    series_id = series['id']
                    logger.info(f"Found series: {series['title']} (ID: {series_id})")
                    
                    # Cancel any existing downloads
                    cancel_all_queue_items(series_id, headers)
                    
                    # Get episodes for the series
                    episodes_response = requests.get(
                        f"{SONARR_URL}/api/v3/episode?seriesId={series_id}",
                        headers=headers
                    )
                    
                    if episodes_response.ok:
                        episodes = episodes_response.json()
                        
                        # Get episode number from tag
                        episode_to_monitor = get_episode_number_from_tag(series, headers)
                        if not episode_to_monitor:
                            return False
                            
                        logger.info(f"Will monitor episode {episode_to_monitor} of season {requested_season}")
                        
                        # Get all episode IDs for the season
                        season_episode_ids = [
                            ep['id'] for ep in episodes 
                            if ep['seasonNumber'] == requested_season
                        ]
                        
                        # Unmonitor all episodes in the season
                        if season_episode_ids:
                            unmonitor_response = requests.put(
                                f"{SONARR_URL}/api/v3/episode/monitor",
                                headers=headers,
                                json={"episodeIds": season_episode_ids, "monitored": False}
                            )
                            if unmonitor_response.ok:
                                logger.info(f"Unmonitored all episodes in season {requested_season}")
                        
                        # Find and monitor only the specific episode
                        target_episode = next(
                            (ep for ep in episodes 
                             if ep['seasonNumber'] == requested_season and 
                             ep['episodeNumber'] == episode_to_monitor),
                            None
                        )
                        
                        if target_episode:
                            monitor_response = requests.put(
                                f"{SONARR_URL}/api/v3/episode/monitor",
                                headers=headers,
                                json={"episodeIds": [target_episode['id']], "monitored": True}
                            )
                            if monitor_response.ok:
                                logger.info(f"Monitoring S{requested_season}E{episode_to_monitor}")
                                return True
                            
                        else:
                            logger.error(f"Target episode S{requested_season}E{episode_to_monitor} not found")
                            return False
                    
            if attempt < max_attempts - 1:
                time.sleep(5)
                
        except Exception as e:
            logger.error(f"Error during setup: {str(e)}")
            if attempt < max_attempts - 1:
                time.sleep(5)
    
    logger.error(f"Series not found after {max_attempts} attempts")
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
    create_episode_tags()

    # Start webhook listener
    logger.info("Starting webhook listener on port 5000")
    app.run(host='0.0.0.0', port=5000)

if __name__ == '__main__':
    main()