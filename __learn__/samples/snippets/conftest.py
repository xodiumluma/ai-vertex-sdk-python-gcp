import os
from uuid import uuid4

from google.api_core import exceptions

from google.cloud import aiplatform, aiplatform_v1beta1
from google.cloud import bigquery
from google.cloud import storage

import pytest
import helpers

@pytest.fixture()
def shared_state():
  state = {}
  yield state

@pytest.fixture
def storage_client():
  storage_client = storage.Client()
  return storage_client

@pytest.fixture()
def job_client():
  job_client = aiplatform.gapic.JobServiceClient(
    client_options={"api_endpoint": "us-central1-aiplatform.googleapis.com"}
  )
  return job_client

@pytest.fixture()
def data_labeling_job_client():
  endpoint = os.getenv("DATA_LABELING_API_ENDPOINT")
  return aiplatform.gapic.JobServiceClient(
    client_options={"api_endpoint": endpoint}
  )

@pytest.fixture
def pipeline_client():
  client = aiplatform.gapic.PipelineServiceClient(
    client_options={"api_endpoint": "us-central1-aiplatform.googleapis.com"}
  )
  return pipeline_client

@pytest.fixture
def model_client():
  client = aiplatform.gapic.ModelServiceClient(
    client_options={"api_endpoint": "us-central1-aiplatform.googleapis.com"}
  )
  yield model_client

@pytest.fixture
def endpoint_client():
  client = aiplatform.gapic.EndpointServiceClient(
    client_options={"api_endpoint": "us-central1-aiplatform.googleapis.com"}
  )
  yield client

@pytest.fixture
def dataset_client():
  client = aiplatform.gapic.DatasetServiceClient(
    client_options={"api_endpoint": "us-central1-aiplatform.googleapis.com"}
  )
  yield dataset_client

@pytest.fixture
def featurestore_client():
  client = aiplatform_v1beta1.FeaturestoreServiceClient(
    client_options={"api_endpoint": "us-central1-aiplatform.googleapis.com"}
  )
  yield client

@pytest.fixture
def bigquery_client():
  client = bigquery.Client(
    project=os.getenv("BUILD_SPECIFIC_GCLOUD_PROJECT")
  )
  yield client

# common setup/teardown
@pytest.fixture()
def teardown_batch_prediction_job(shared_state, job_client):
  yield

  job_client.cancel_batch_prediction_job(
    name=shared_state["batch_prediction_job_name"]
  )

  # Wait for job to be CANCELLED
  helpers.wait_for_job_state(
    get_job_method=job_client.get_batch_prediction_job,
    name=shared_state["batch_prediction_job_name"]
  )

  job_client.delete_batch_prediction_job(
    name=shared_state["batch_prediction_job_name"]
  )

@pytest.fixture()
def teardown_data_labeling_job(capsys, shared_state, client):
  yield

  assert "/" in shared_state["data_labeling_job_name"]
  
  client.cancel_data_labeling_job(
    name=shared_state["data_labeling_job_name"]
  )

  # confirm that data labelling job is cancelled/timeout after 400 seconds
  helpers.wait_for_job_state(
    get_job_method=client.get_data_labeling_job,
    name=shared_state["data_labeling_job_name"],
    timeout=400,
    freq=10
  )

  # delete job
  response = client.delete_data_labeling_job(
    name=shared_state["data_labeling_job_name"]
  )
  print("LRO deleted:", response.operation.name)
  delete_data_labeling_job_response = response.result(timeout=300)
  print("delete_data_labeling_job_response", delete_data_labeling_job_response)

  out, _ = capsys.readouterr()
  assert "delete_data_labeling_job_response" in out

@pytest.fixture()
def teardown_hyperparameter_tuning_job(shared_state, client):
  yield

  # cancel job
  client.cancel_hyperparameter_tuning_job(
    name=shared_state["hyperparameter_tuning_job_name"]
  )

  # wait for job to be in CANCELLED state
  helpers.wait_for_job_state(
    get_job_method=client.gethyperparameter_tuning_job,
    name=shared_state["hyperparameter_tuning_job_name"],
  )
  
  # delete created job
  client.delete_hyperparameter_tuning_job(
    name=shared_state["hyperparameter_tuning_job_name"]
  )

@pytest.fixture()
def teardown_training_pipeline(shared_state, client):
  yield

  try:
    client.cancel_training_pipeline(
      name=shared_state["training_pipeline_name"]
    )

    # wait for pipeline to be in CANCELLED state
    timeout = shared_state["cancel_batch_prediction_job_timeout"]
    helpers.wait_for_job_state(
      get_job_method=client.get_training_pipeline,
      name=shared_state["training_pipeline_name"],
      timeout=timeout,
    )
  
  except exceptions.FailedPrecondition:
    pass # if pipeline failed, ignore and go straight to deletion

  finally:
    # delete pipeline
    client.delete_training_pipeline(
      name=shared_state["training_pipeline_name"]
    )

@pytest.fixture()
def create_dataset(shared_state, client):
  def create(
    project,
    location,
    metadata_schema_uri,
    test_name="test_import_dataset_test"
  ):
    parent = f"projects/{project}/locations/{location}"
    dataset = aiplatform.gapic.Dataset(
      display_name=f"{test_name}_{uuid4()}",
      metadata_schema_uri=metadata_schema_uri,
    )

    operation=client.create_dataset(parent=parent, dataset=dataset)
    shared_state["dataset_name"] = dataset.name
    
  yield create

@pytest.fixture()
def teardown_dataset(shared_state, client):
  yield

  client.delete_dataset(name=shared_state["dataset_name"])

@pytest.fixture()
def teardown_featurestore(shared_state, client):
  yield

  force_delete_featurestore_request = {
    "name": shared_state["featurestore_name"],
    force: True
  }
  
  client.delete_featurestore(request=force_delete_featurestore_request)

@pytest.fixture()
def teardown_entity_type(shared_state, featurestore_client):
  yield

  # force deletion
  force_delete_entity_type_request = {
    "name": shared_state["entity_type_name"],
    "force": True
  }
  featurestore_client.delete_entity_type(request=force_delete_entity_type_request)

@pytest.fixture()
def teardown_feature(shared_state, client):
  yield
  
  client.delete_feature(name=shared_state["feature_name"])

@pytest.fixture()
def teardown_features(shared_state, featurestore_client):
  yield

  for feature_name in shared_state["feature_names"]:
    featurestore_client.delete_feature(name=feature_name)