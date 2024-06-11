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