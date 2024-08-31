from google.cloud import aiplatform

def create_dataset_video_sample(
  project: str,
  display_name: str,
  location: str = "us-central1",
  api_endpoint: str = "us-central1-aiplatform.googleapis.com",
  timeout: int = 300,
):
  # regional endpoints required
  client_options = {"api_endpoint": api_endpoint}
  # instantiate reusable client
  client = aiplatform.gapic.DatasetServiceClient(client_options=client_options)
  dataset = {
    "display_name": display_name,
    "metadata_schema_uri": "gs://google-cloud-aiplatform/schema/dataset/metadata/video_1.0.0.yaml",
  }
  parent = f"projects/{project}/locations/{location}"
  response = client.create_dataset(parent=parent, dataset=dataset)
  print("Lengthy process:", response.operation.name)
  create_dataset_response = response.result(timeout=timeout)
  print("create_dataset_response:", create_dataset_response)

# [END aiplatform_create_dataset_video_sample]
