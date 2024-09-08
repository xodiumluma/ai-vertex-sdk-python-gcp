# [START aiplatform_delete_dataset_sample]
from google.cloud import aiplatform

def delete_dataset_sample(
  project: str,
  dataset_id: str,
  location: str = "us-central1",
  api_endpoint: str = "us-central1-aiplatform.googleapis.com",
  timeout: int = 300,
):
  # regional API endpoints needed
  client_options = {"api_endpoint": api_endpoint}
  # initialise reusable client
  client = aiplatform.gapic.DatasetServiceClient(client_options=client_options)
  name = client.dataset_path(project=project, location=location, dataset=dataset_id)
  response = client.delete_dataset(name=name)
  print("Lengthy operation:", response.operation.name)
  delete_dataset_response = response.result(timeout=timeout)
  print("delete_dataset_response:", delete_dataset_response)
  
# [END aiplatform_delete_dataset_sample]
