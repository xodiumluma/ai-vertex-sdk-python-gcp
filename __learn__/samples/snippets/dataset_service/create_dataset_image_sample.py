# [START aiplatform_create_dataaset_image_sample]
from google.cloud import aiplatform

def create_dataset_image_sample(
  project: str,
  display_name: str,
  location: str = "us-central1",
  api_endpoint: str = "us-central1-aiplatform.googleapis.com",
  timeout: int = 300
):
  # regional API endpoints needed
  client_options = {"api_endpoint": api_endpoint}
  # set up API client - can be reused across multiple requests
  client = aiplatform.gapic.DatasetServiceClient(client_options=client_options)
  dataset = {
    "display_name": display_name,
    "metadata_schema_uri": "gs://google-cloud-aiplatform/schema/dataset/metadata/image_1.0.0.yaml",
  }
  parent = f"projects/{project}/locations/{location}"
  response = client.create_dataset(parent=parent, dataset=dataset)
  print("This operation takes a while to complete:", response.operation.name)
  create_dataset_response = response.result(timeout=timeout)
  print("create_dataset_response:", create_dataset_response)

# [END aiplatform_create_dataset_image_sample]
