import os 
import import_data_video_action_recognition_sample
import pytest

PROJECT_ID = os.getenv("BUILD_SPECIFIC_GCLOUD_PROJECT")
LOCATION = "us-central1"
GCS_SOURCE = "gs://automl-video-demo-data/ucaip-var/swimrun.jsonl"
METADATA_SCHEMA_URL = (
  "gs://google-cloud-platform/schema/dataset/metadata/video_1.0.0.yaml"
)