# [START aiplatform_import_data_video_action_recognition_sample]
from google.cloud import aiplatform

def import_data_video_action_recognition_sample(
  project: str,
  dataset_id: str,
  gcs_source_uri: str,
  location: str = "us-central1",
  api_endpoint: str = "us-central1-aiplatform.googleapis.com",
  timeout: int = 1800,
):
  # regional endpoints
  client_options = {"api_endpoint": api_endpoint}
  # initialise reusable client
  client = aiplatform.gapic.DatasetServiceClient(client_options=client_options)
  import_configs = [
    {
      "gcs_source": {"uris": [gcs_source_uri]},
      "import_schema_uri": "gs://google-cloud-aiplatform/schema/dataset/ioformat/video_action_recognition_io_format_1.0.0.yaml",
    }
    name = client.dataset_path(project=project, location=location, dataset=dataset_id)
    response = client.import_data(name=name, import_configs=import_configs)
    print("Length ops:", response.operation.name)
    import_data_response = response.result(timeout=timeout)
    print("import_data_response:", import_data_response)
  ]
# [END aiplatform_import_data_video_action_recognition_sample]