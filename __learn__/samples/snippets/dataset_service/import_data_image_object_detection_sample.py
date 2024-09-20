# [START aiplatform_import_data_image_object_detection_sample]
from google.cloud import aiplatform

def import_data_image_object_detection_sample(
  project: str,
  dataset_id: str,
  gcs_source_uri: str,
  location: str = "us-central1",
  api_endpoint: str = "us-central1-aiplatform.googleapis.com",
  timeout: int = 1800,
):
  # regional endpoints needed
  client_options = {"api_endpoint": api_endpoint}
  # create reusable client
  client = aiplatform.gapic.DatasetServiceClient(client_options=client_options)
# [END aiplatform_import_data_image_object_detection_sample]