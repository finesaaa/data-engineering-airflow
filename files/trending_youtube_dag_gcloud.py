from airflow.decorators import dag, task
from googleapiclient.discovery import build
from google.cloud import bigquery
from google.oauth2 import service_account
import json
from datetime import datetime, timedelta, timezone
import isodate
import os
from dotenv import load_dotenv

default_args = {
    'owner': 'tmtsmrsl',
    'retries': 3,
    'retry_delay': timedelta(minutes=5)
}

@dag(dag_id='trending_youtube_dag_v1',
    default_args=default_args,
    description='A pipeline to fetch trending YouTube videos',
    start_date=datetime(2023, 5, 7, tzinfo=timezone(timedelta(hours=7))),
    schedule_interval='0 10 * * *',
    catchup=False)
def trending_youtube_dag():
    @task()
    def fetch_trending_videos(region_code: str, max_results: int, target_file_path: str):
        """Fetches trending videos from YouTube for a specific region.

        Args:
            region_code: A string representing the ISO 3166-1 alpha-2 country code for the desired region.
            max_results: An integer representing the maximum number of results to fetch.
            target_file_path: A string representing the path to the file to be written.
        """
        
        # Load API key from .env file
        load_dotenv("/opt/airflow/dags/.env")
        api_key = os.environ.get("YOUTUBE_API_KEY")

        # Create YouTube API client
        youtube = build("youtube", "v3", developerKey=api_key)

        # Fetch videos until max_results is reached or there are no more results
        videos_list = []
        next_page_token = ""
        while len(videos_list) < max_results and next_page_token is not None:
            # Make API request for videos
            request = youtube.videos().list(
                part="snippet,contentDetails,statistics",
                chart="mostPopular",
                regionCode=region_code,
                maxResults=50,
                pageToken=next_page_token,
            )
            response = request.execute()

            # Extract videos from response
            videos = response.get("items", [])

            # Update next_page_token for the next API request
            next_page_token = response.get("nextPageToken", None)
            
            # Extract relevant video details and append to list
            infos = {'snippet':['title', 'publishedAt', 'channelId', 'channelTitle',
                                'description', 'tags', 'thumbnails', 'categoryId', 'defaultAudioLanguage'],
                        'contentDetails':['duration', 'caption'],
                        'statistics':['viewCount', 'likeCount', 'commentCount']}
            now = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
            for video in videos:
                video_details = {
                    'videoId': video["id"],
                    'trendingAt': now
                }
                
                for k in infos.keys():
                    for info in infos[k]:
                        # use try-except to handle missing info
                        try:
                            video_details[info] = video[k][info]
                        except KeyError:
                            video_details[info] = None
                            
                videos_list.append(video_details)

        # Write fetched videos data to a json file
        with open(target_file_path, "w") as f:
            json.dump(videos_list, f)
    
    @task()
    def data_processing(source_file_path: str, target_file_path: str):
        """Processes the raw data fetched from YouTube.
        
        Args:
            source_file_path: A string representing the path to the file to be processed.
            target_file_path: A string representing the path to the file to be written.
        """
        # Load the fetched videos data from the json file
        with open(source_file_path, 'r') as f:
            videos_list = json.load(f)
        
        # Load the categories dictionary from the json file
        with open('/opt/airflow/dags/categories.json', 'r') as f:
            categories = json.load(f)
        
        # Process the fetched videos data
        for video in videos_list:
            # Convert ISO 8601 duration to seconds
            video['durationSec'] = int(isodate.parse_duration(video['duration']).total_seconds()) if video['duration'] is not None else None
            del video['duration']
            
            # Convert tags list to string
            video['tags'] = ', '.join(video['tags']) if video['tags'] is not None else None
            
            # Convert categoryId to category based on categories dictionary
            video['category'] = categories.get(video['categoryId'], None) if video['categoryId'] is not None else None
            del video['categoryId']

            # Parse the thumbnail url
            video['thumbnailUrl'] = video['thumbnails'].get('standard', {}).get('url', None) if video['thumbnails'] is not None else None
            del video['thumbnails']
            
            # Convert viewCount, likeCount, and commentCount to integer
            video['viewCount'] = int(video['viewCount']) if video['viewCount'] is not None else None
            video['likeCount'] = int(video['likeCount']) if video['likeCount'] is not None else None
            video['commentCount'] = int(video['commentCount']) if video['commentCount'] is not None else None
            
            # Convert caption to boolean
            video['caption'] = True if video['caption'] == 'true' else False if video['caption'] == 'false' else None
        
        # Save the processed videos data to a new file
        with open(target_file_path, "w") as f:
            json.dump(videos_list, f)
            
    @task()
    def load_to_bigquery(source_file_path: str, table_name: str):
        """
        Loads the processed data to BigQuery.
        
        Args:
            source_file_path: A string representing the path to the file to be loaded.
            table_name: A string representing the name of the table to load the data to.
        """
        
        # Set the path to your service account key file
        key_path = '/opt/airflow/dags/service_account_key.json'

        # Set the credentials using the service account key
        credentials = service_account.Credentials.from_service_account_file(
            key_path,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )

        # Instantiate the BigQuery client with the credentials
        client = bigquery.Client(credentials=credentials)
        
        # Refer to the table where the data will be loaded
        dataset_ref = client.dataset('youtube')
        table_ref = dataset_ref.table(table_name)
        table = client.get_table(table_ref)
        
        # Load the data from the json file to BigQuery
        with open(source_file_path, 'r') as f:
            json_data = json.load(f)
        job_config = bigquery.LoadJobConfig()
        job_config.source_format = bigquery.SourceFormat.NEWLINE_DELIMITED_JSON
        job = client.load_table_from_json(json_data, table, job_config = job_config)
        job.result() # Waits for the job to complete
    
        # Log the job results
        print(f"Loaded {job.output_rows} rows to {table.table_id}")
    
    file_path = '/opt/airflow/dags/tmp_file.json'
    fetch_trending_videos_task = fetch_trending_videos(region_code='ID', max_results=200, target_file_path=file_path)
    processed_file_path = '/opt/airflow/dags/tmp_file_processed.json'
    data_processing_task = data_processing(source_file_path=file_path, target_file_path=processed_file_path)
    load_to_bigquery_task = load_to_bigquery(source_file_path=processed_file_path, table_name='trending_videos')
    
    fetch_trending_videos_task >> data_processing_task >> load_to_bigquery_task
    
dag = trending_youtube_dag()