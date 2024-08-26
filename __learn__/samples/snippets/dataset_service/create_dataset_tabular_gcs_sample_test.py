import os
from uuid import uuid4

import create_dataset_tabular_gcs_sample
import pytest_lazyfixture

import helpers

PROJECT_ID = os.getenv("BUILD_SPECIFIC_GCLOUD_PROJECT")
GCS_URI = "gs://ucaip-sample-resources/iris_1000.csv"

