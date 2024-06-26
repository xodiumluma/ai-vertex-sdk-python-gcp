# -*- coding: utf-8 -*-

# Copyright 2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import datetime
import time
from typing import Dict, List, Optional, Sequence, Tuple, Union
from google.protobuf import json_format

import abc

from google.auth import credentials as auth_credentials
from google.cloud.aiplatform import base
from google.cloud.aiplatform.constants import base as constants
from google.cloud.aiplatform import datasets
from google.cloud.aiplatform import explain
from google.cloud.aiplatform import hyperparameter_tuning as hpt
from google.cloud.aiplatform import initializer
from google.cloud.aiplatform import models
from google.cloud.aiplatform import jobs
from google.cloud.aiplatform import schema
from google.cloud.aiplatform import utils
from google.cloud.aiplatform.utils import console_utils

from google.cloud.aiplatform.compat.types import env_var as gca_env_var
from google.cloud.aiplatform.compat.types import io as gca_io
from google.cloud.aiplatform.compat.types import model as gca_model
from google.cloud.aiplatform.compat.types import (
    pipeline_state as gca_pipeline_state,
)
from google.cloud.aiplatform.compat.types import (
    training_pipeline as gca_training_pipeline,
    study as gca_study_compat,
)

from google.cloud.aiplatform.utils import _timestamped_gcs_dir
from google.cloud.aiplatform.utils import source_utils
from google.cloud.aiplatform.utils import worker_spec_utils
from google.cloud.aiplatform.utils import column_transformations_utils
from google.cloud.aiplatform.utils import _explanation_utils

from google.cloud.aiplatform.v1.schema.trainingjob import (
    definition_v1 as training_job_inputs,
)

from google.rpc import code_pb2
from google.rpc import status_pb2

import proto


_LOGGER = base.Logger(__name__)

_PIPELINE_COMPLETE_STATES = set(
    [
        gca_pipeline_state.PipelineState.PIPELINE_STATE_SUCCEEDED,
        gca_pipeline_state.PipelineState.PIPELINE_STATE_FAILED,
        gca_pipeline_state.PipelineState.PIPELINE_STATE_CANCELLED,
        gca_pipeline_state.PipelineState.PIPELINE_STATE_PAUSED,
    ]
)

# _block_until_complete wait times
_JOB_WAIT_TIME = 5  # start at five seconds
_LOG_WAIT_TIME = 5
_MAX_WAIT_TIME = 60 * 5  # 5 minute wait
_WAIT_TIME_MULTIPLIER = 2  # scale wait by 2 every iteration


class _TrainingJob(base.VertexAiStatefulResource):

    client_class = utils.PipelineClientWithOverride
    _resource_noun = "trainingPipelines"
    _getter_method = "get_training_pipeline"
    _list_method = "list_training_pipelines"
    _delete_method = "delete_training_pipeline"
    _parse_resource_name_method = "parse_training_pipeline_path"
    _format_resource_name_method = "training_pipeline_path"

    # Required by the done() method
    _valid_done_states = _PIPELINE_COMPLETE_STATES

    def __init__(
        self,
        display_name: Optional[str] = None,
        project: Optional[str] = None,
        location: Optional[str] = None,
        credentials: Optional[auth_credentials.Credentials] = None,
        labels: Optional[Dict[str, str]] = None,
        training_encryption_spec_key_name: Optional[str] = None,
        model_encryption_spec_key_name: Optional[str] = None,
    ):
        """Constructs a Training Job.

        Args:
            display_name (str):
                Optional. The user-defined name of the training pipeline. The
                name must contain 128 or fewer UTF-8 characters.
            project (str):
                Optional. The name of a Google Cloud project from which to
                retrieve the model. This overrides the project that was set by
                `aiplatform.init`.
            location (str):
                Optional. The Google Cloud region where this from where the
                model is retrieved. This region overrides the region that was
                set by `aiplatform.init`.
            credentials (auth_credentials.Credentials):
                Optional. The credentials that are used to retrieve the model.
                These credentials override the credentials set by
                `aiplatform.init`.
            labels (Dict[str, str]):
                Optional. Labels with user-defined metadata to organize your
                training pipeline. The maximum length of a key and value is 64
                unicode characters. Labels and keys can contain only lowercase
                letters, numeric characters, underscores, and dashes.
                International characters are allowed. No more than 64 user
                labels can be associated with one Tensorboard (system labels are
                excluded). For more information and examples of using labels,
                see [Using labels to organize Google Cloud Platform
                resources](https://goo.gl/xmQnxf). System reserved label keys
                are prefixed with `aiplatform.googleapis.com/` and are
                immutable.
            training_encryption_spec_key_name (Optional[str]):
                Optional. The Cloud KMS resource identifier of the customer
                managed encryption key that's used to protect the training
                pipeline. The format of the key is
                `projects/my-project/locations/my-region/keyRings/my-kr/cryptoKeys/my-key`.
                The key needs to be in the same region as where the compute
                resource is created.

                If `training_encryption_spec_key_name` is set, this training
                pipeline is secured by this key. The model trained by this
                training pipeline is also secured by this key if
                `model_to_upload` isn't set.

                This `training_encryption_spec_key_name` overrides the
                `encryption_spec_key_name` set by `aiplatform.init`.
            model_encryption_spec_key_name (Optional[str]):
                Optional. The Cloud KMS resource identifier of the customer
                managed encryption key that's used to protect the model. The
                format of the key is
                `projects/my-project/locations/my-region/keyRings/my-kr/cryptoKeys/my-key`.
                The key needs to be in the same region as where the compute
                resource is created.

                If `model_encryption_spec_key_name` is set, the traind model is
                secured by this key.

                This `model_encryption_spec_key_name` overrides the
                `encryption_spec_key_name` set by `aiplatform.init`.
        """
        if not display_name:
            display_name = self.__class__._generate_display_name()
        utils.validate_display_name(display_name)
        if labels:
            utils.validate_labels(labels)

        super().__init__(project=project, location=location, credentials=credentials)
        self._display_name = display_name
        self._labels = labels
        self._training_encryption_spec = initializer.global_config.get_encryption_spec(
            encryption_spec_key_name=training_encryption_spec_key_name
        )
        self._model_encryption_spec = initializer.global_config.get_encryption_spec(
            encryption_spec_key_name=model_encryption_spec_key_name
        )
        self._gca_resource = None

    @property
    @classmethod
    @abc.abstractmethod
    def _supported_training_schemas(cls) -> Tuple[str]:
        """The list of supported schemas for this training job."""
        pass

    @property
    def start_time(self) -> Optional[datetime.datetime]:
        """Optional. The time when the training job first entered the
        `PIPELINE_STATE_RUNNING` state."""
        self._sync_gca_resource()
        return getattr(self._gca_resource, "start_time")

    @property
    def end_time(self) -> Optional[datetime.datetime]:
        """Optional. The time when the training job entered the
        `PIPELINE_STATE_SUCCEEDED`, `PIPELINE_STATE_FAILED`, or
        `PIPELINE_STATE_CANCELLED` state."""
        self._sync_gca_resource()
        return getattr(self._gca_resource, "end_time")

    @property
    def error(self) -> Optional[status_pb2.Status]:
        """Optional. Detailed error information for this training job resource.
        Error information is created only when the state of the training job is
        `PIPELINE_STATE_FAILED` or `PIPELINE_STATE_CANCELLED`."""
        self._sync_gca_resource()
        return getattr(self._gca_resource, "error")

    @classmethod
    def get(
        cls,
        resource_name: str,
        project: Optional[str] = None,
        location: Optional[str] = None,
        credentials: Optional[auth_credentials.Credentials] = None,
    ) -> "_TrainingJob":
        """Gets a training job using the `resource_name` that's passed in.

        Args:
            resource_name (str):
                Required. A fully-qualified resource name or ID.
            project (str):
                Optional. The name of the Google Cloud project to retrieve the
                training job from. This overrides the project that was set by
                `aiplatform.init`.
            location (str):
                Optional. The Google Cloud region from where the training job is
                retrieved. This region overrides the region that was set by
                `aiplatform.init`.
            credentials (auth_credentials.Credentials):
                Optional. The credentials that are used to upload this model.
                These credentials override the credentials set by
                `aiplatform.init`.

        Raises:
            ValueError: A `ValueError` is raised if the task definition of the
                retrieved training job doesn't match the custom training task
                definition.

        Returns:
            A Vertex AI training job.
        """

        # Create job with dummy parameters
        # These parameters won't be used as user can not run the job again.
        # If they try, an exception will be raised.
        self = cls._empty_constructor(
            project=project,
            location=location,
            credentials=credentials,
            resource_name=resource_name,
        )

        self._gca_resource = self._get_gca_resource(resource_name=resource_name)

        if (
            self._gca_resource.training_task_definition
            not in cls._supported_training_schemas
        ):
            raise ValueError(
                f"The task definition of the retrieved training job "
                f"is {self._gca_resource.training_task_definition}, "
                f"which is not compatible with {cls.__name__}."
            )

        return self

    @classmethod
    def _get_and_return_subclass(
        cls,
        resource_name: str,
        project: Optional[str] = None,
        location: Optional[str] = None,
        credentials: Optional[auth_credentials.Credentials] = None,
    ) -> "_TrainingJob":
        """Retrieves a training job subclass for the `resource_name` that's
        passed in without knowing the `training_task_definition`.

        Example usage:
        ```
        aiplatform.training_jobs._TrainingJob._get_and_return_subclass(
            'projects/.../locations/.../trainingPipelines/12345'
        )
        # Returns: <google.cloud.aiplatform.training_jobs.AutoMLImageTrainingJob>
        ```

        Args:
            resource_name (str):
                Required. A fully-qualified resource name or ID.
            project (str):
                Optional. The name of the Google Cloud project to retrieve the
                training job from. This overrides the project that was set by
                `aiplatform.init`.
            location (str):
                Optional. The Google Cloud region from where the training job is
                retrieved. This region overrides the region that was set by
                `aiplatform.init`.
            credentials (auth_credentials.Credentials):
                Optional. The credentials that are used to upload this model.
                These credentials override the credentials set by
                `aiplatform.init`.

        Returns:
            A Vertex AI training job.
        """

        # Retrieve training pipeline resource before class construction
        client = cls._instantiate_client(location=location, credentials=credentials)

        gca_training_pipeline = getattr(client, cls._getter_method)(name=resource_name)

        schema_uri = gca_training_pipeline.training_task_definition

        # Collect all AutoML training job classes and CustomTrainingJob
        class_list = [
            c for c in cls.__subclasses__() if c.__name__.startswith("AutoML")
        ] + [CustomTrainingJob]

        # Identify correct training job subclass, construct and return object
        for c in class_list:
            if schema_uri in c._supported_training_schemas:
                return c._empty_constructor(
                    project=project,
                    location=location,
                    credentials=credentials,
                    resource_name=resource_name,
                )

    @property
    @abc.abstractmethod
    def _model_upload_fail_string(self) -> str:
        """Helper property for model upload failure."""
        pass

    @abc.abstractmethod
    def run(self) -> Optional[models.Model]:
        """Runs the training job.

        Should call _run_job internally
        """
        pass

    @staticmethod
    def _create_input_data_config(
        dataset: Optional[datasets._Dataset] = None,
        annotation_schema_uri: Optional[str] = None,
        training_fraction_split: Optional[float] = None,
        validation_fraction_split: Optional[float] = None,
        test_fraction_split: Optional[float] = None,
        training_filter_split: Optional[str] = None,
        validation_filter_split: Optional[str] = None,
        test_filter_split: Optional[str] = None,
        predefined_split_column_name: Optional[str] = None,
        timestamp_split_column_name: Optional[str] = None,
        gcs_destination_uri_prefix: Optional[str] = None,
        bigquery_destination: Optional[str] = None,
    ) -> Optional[gca_training_pipeline.InputDataConfig]:
        """Constructs an input data config to pass to the training pipeline.

        Args:
            dataset (datasets._Dataset):
                The dataset within the same Project from which data will be used to train the Model. The
                Dataset must use schema compatible with Model being trained,
                and what is compatible should be described in the used
                TrainingPipeline's [training_task_definition]
                [google.cloud.aiplatform.v1beta1.TrainingPipeline.training_task_definition].
                For tabular Datasets, all their data is exported to
                training, to pick and choose from.
            annotation_schema_uri (str):
                Google Cloud Storage URI points to a YAML file describing
                annotation schema. The schema is defined as an OpenAPI 3.0.2
                [Schema Object](https://github.com/OAI/OpenAPI-Specification/blob/main/versions/3.0.2.md#schema-object) The schema files
                that can be used here are found in
                gs://google-cloud-aiplatform/schema/dataset/annotation/,
                note that the chosen schema must be consistent with
                ``metadata``
                of the Dataset specified by
                ``dataset_id``.

                Only Annotations that both match this schema and belong to
                DataItems not ignored by the split method are used in
                respectively training, validation or test role, depending on
                the role of the DataItem they are on.

                When used in conjunction with
                ``annotations_filter``,
                the Annotations used for training are filtered by both
                ``annotations_filter``
                and
                ``annotation_schema_uri``.
            training_fraction_split (float):
                Optional. The fraction of the input data that is to be used to train
                the Model. This is ignored if Dataset is not provided.
            validation_fraction_split (float):
                Optional. The fraction of the input data that is to be used to validate
                the Model. This is ignored if Dataset is not provided.
            test_fraction_split (float):
                Optional. The fraction of the input data that is to be used to evaluate
                the Model. This is ignored if Dataset is not provided.
            training_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to train the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            validation_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to validate the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            test_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to test the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            predefined_split_column_name (str):
                Optional. The key is a name of one of the Dataset's data
                columns. The value of the key (either the label's value or
                value in the column) must be one of {``training``,
                ``validation``, ``test``}, and it defines to which set the
                given piece of data is assigned. If for a piece of data the
                key is not present or has an invalid value, that piece is
                ignored by the pipeline.

                Supported only for tabular and time series Datasets.
            timestamp_split_column_name (str):
                Optional. The key is a name of one of the Dataset's data
                columns. The value of the key values of the key (the values in
                the column) must be in RFC 3339 `date-time` format, where
                `time-offset` = `"Z"` (e.g. 1985-04-12T23:20:50.52Z). If for a
                piece of data the key is not present or has an invalid value,
                that piece is ignored by the pipeline.

                Supported only for tabular and time series Datasets.
                This parameter must be used with training_fraction_split,
                validation_fraction_split, and test_fraction_split.
            gcs_destination_uri_prefix (str):
                Optional. The Google Cloud Storage location.

                The Vertex AI environment variables representing Google
                Cloud Storage data URIs will always be represented in the
                Google Cloud Storage wildcard format to support sharded
                data.

                -  AIP_DATA_FORMAT = "jsonl".
                -  AIP_TRAINING_DATA_URI = "gcs_destination/training-*"
                -  AIP_VALIDATION_DATA_URI = "gcs_destination/validation-*"
                -  AIP_TEST_DATA_URI = "gcs_destination/test-*".
            bigquery_destination (str):
                The BigQuery project location where the training data is to
                be written to. In the given project a new dataset is created
                with name
                ``dataset_<dataset-id>_<annotation-type>_<timestamp-of-training-call>``
                where timestamp is in YYYY_MM_DDThh_mm_ss_sssZ format. All
                training input data will be written into that dataset. In
                the dataset three tables will be created, ``training``,
                ``validation`` and ``test``.

                -  AIP_DATA_FORMAT = "bigquery".
                -  AIP_TRAINING_DATA_URI ="bigquery_destination.dataset_*.training"
                -  AIP_VALIDATION_DATA_URI = "bigquery_destination.dataset_*.validation"
                -  AIP_TEST_DATA_URI = "bigquery_destination.dataset_*.test"
        Raises:
            ValueError: When more than 1 type of split configuration is passed or when
                the split configuration passed is incompatible with the dataset schema.
        """

        input_data_config = None
        if dataset:
            # Initialize all possible splits
            filter_split = None
            predefined_split = None
            timestamp_split = None
            fraction_split = None

            # Create filter split
            if any(
                [
                    training_filter_split is not None,
                    validation_filter_split is not None,
                    test_filter_split is not None,
                ]
            ):
                if all(
                    [
                        training_filter_split is not None,
                        validation_filter_split is not None,
                        test_filter_split is not None,
                    ]
                ):
                    filter_split = gca_training_pipeline.FilterSplit(
                        training_filter=training_filter_split,
                        validation_filter=validation_filter_split,
                        test_filter=test_filter_split,
                    )
                else:
                    raise ValueError(
                        "All filter splits must be passed together or not at all"
                    )

            # Create predefined split
            if predefined_split_column_name:
                predefined_split = gca_training_pipeline.PredefinedSplit(
                    key=predefined_split_column_name
                )

            # Create timestamp split or fraction split
            if timestamp_split_column_name:
                timestamp_split = gca_training_pipeline.TimestampSplit(
                    training_fraction=training_fraction_split,
                    validation_fraction=validation_fraction_split,
                    test_fraction=test_fraction_split,
                    key=timestamp_split_column_name,
                )
            elif any(
                [
                    training_fraction_split is not None,
                    validation_fraction_split is not None,
                    test_fraction_split is not None,
                ]
            ):
                fraction_split = gca_training_pipeline.FractionSplit(
                    training_fraction=training_fraction_split,
                    validation_fraction=validation_fraction_split,
                    test_fraction=test_fraction_split,
                )

            splits = [
                split
                for split in [
                    filter_split,
                    predefined_split,
                    timestamp_split_column_name,
                    fraction_split,
                ]
                if split is not None
            ]

            # Fallback to fraction split if nothing else is specified
            if len(splits) == 0:
                _LOGGER.info(
                    "No dataset split provided. The service will use a default split."
                )
            elif len(splits) > 1:
                raise ValueError(
                    "You've specified too many split types. You can specify"
                    " only one of the following:"
                    "    1. `training_filter_split`, `validation_filter_split`,"
                    " `test_filter_split`"
                    "    2. `predefined_split_column_name`"
                    "    3. `timestamp_split_column_name`,"
                    " `training_fraction_split`, `validation_fraction_split`,"
                    " `test_fraction_split`"
                    "    4.`training_fraction_split`,"
                    " `validation_fraction_split`, `test_fraction_split`"
                )

            # create GCS destination
            gcs_destination = None
            if gcs_destination_uri_prefix:
                gcs_destination = gca_io.GcsDestination(
                    output_uri_prefix=gcs_destination_uri_prefix
                )

            # TODO(b/177416223) validate managed BQ dataset is passed in
            bigquery_destination_proto = None
            if bigquery_destination:
                bigquery_destination_proto = gca_io.BigQueryDestination(
                    output_uri=bigquery_destination
                )

            # create input data config
            input_data_config = gca_training_pipeline.InputDataConfig(
                fraction_split=fraction_split,
                filter_split=filter_split,
                predefined_split=predefined_split,
                timestamp_split=timestamp_split,
                dataset_id=dataset.name,
                annotation_schema_uri=annotation_schema_uri,
                gcs_destination=gcs_destination,
                bigquery_destination=bigquery_destination_proto,
            )

        return input_data_config

    def _run_job(
        self,
        training_task_definition: str,
        training_task_inputs: Union[dict, proto.Message],
        dataset: Optional[datasets._Dataset],
        training_fraction_split: Optional[float] = None,
        validation_fraction_split: Optional[float] = None,
        test_fraction_split: Optional[float] = None,
        training_filter_split: Optional[str] = None,
        validation_filter_split: Optional[str] = None,
        test_filter_split: Optional[str] = None,
        predefined_split_column_name: Optional[str] = None,
        timestamp_split_column_name: Optional[str] = None,
        annotation_schema_uri: Optional[str] = None,
        model: Optional[gca_model.Model] = None,
        model_id: Optional[str] = None,
        parent_model: Optional[str] = None,
        is_default_version: Optional[bool] = True,
        model_version_aliases: Optional[Sequence[str]] = None,
        model_version_description: Optional[str] = None,
        gcs_destination_uri_prefix: Optional[str] = None,
        bigquery_destination: Optional[str] = None,
        create_request_timeout: Optional[float] = None,
        block: Optional[bool] = True,
    ) -> Optional[models.Model]:
        """Runs the training job.

        Args:
            training_task_definition (str):
                Required. A Google Cloud Storage path to the
                YAML file that defines the training task which
                is responsible for producing the model artifact,
                and may also include additional auxiliary work.
                The definition files that can be used here are
                found in gs://google-cloud-
                aiplatform/schema/trainingjob/definition/. Note:
                The URI given on output will be immutable and
                probably different, including the URI scheme,
                than the one given on input. The output URI will
                point to a location where the user only has a
                read access.
            training_task_inputs (Union[dict, proto.Message]):
                Required. The training task's input that corresponds to the training_task_definition parameter.
            dataset (datasets._Dataset):
                The dataset within the same Project from which data will be used to train the Model. The
                Dataset must use schema compatible with Model being trained,
                and what is compatible should be described in the used
                TrainingPipeline's [training_task_definition]
                [google.cloud.aiplatform.v1beta1.TrainingPipeline.training_task_definition].
                For tabular Datasets, all their data is exported to
                training, to pick and choose from.
            annotation_schema_uri (str):
                Google Cloud Storage URI points to a YAML file describing
                annotation schema. The schema is defined as an OpenAPI 3.0.2
                [Schema Object](https://github.com/OAI/OpenAPI-Specification/blob/main/versions/3.0.2.md#schema-object) The schema files
                that can be used here are found in
                gs://google-cloud-aiplatform/schema/dataset/annotation/,
                note that the chosen schema must be consistent with
                ``metadata``
                of the Dataset specified by
                ``dataset_id``.

                Only Annotations that both match this schema and belong to
                DataItems not ignored by the split method are used in
                respectively training, validation or test role, depending on
                the role of the DataItem they are on.

                When used in conjunction with
                ``annotations_filter``,
                the Annotations used for training are filtered by both
                ``annotations_filter``
                and
                ``annotation_schema_uri``.
            training_fraction_split (float):
                Optional. The fraction of the input data that is to be used to train
                the Model. This is ignored if Dataset is not provided.
            validation_fraction_split (float):
                Optional. The fraction of the input data that is to be used to validate
                the Model. This is ignored if Dataset is not provided.
            test_fraction_split (float):
                Optional. The fraction of the input data that is to be used to evaluate
                the Model. This is ignored if Dataset is not provided.
            training_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to train the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            validation_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to validate the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            test_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to test the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            predefined_split_column_name (str):
                Optional. The key is a name of one of the Dataset's data
                columns. The value of the key (either the label's value or
                value in the column) must be one of {``training``,
                ``validation``, ``test``}, and it defines to which set the
                given piece of data is assigned. If for a piece of data the
                key is not present or has an invalid value, that piece is
                ignored by the pipeline.

                Supported only for tabular and time series Datasets.
            timestamp_split_column_name (str):
                Optional. The key is a name of one of the Dataset's data
                columns. The value of the key values of the key (the values in
                the column) must be in RFC 3339 `date-time` format, where
                `time-offset` = `"Z"` (e.g. 1985-04-12T23:20:50.52Z). If for a
                piece of data the key is not present or has an invalid value,
                that piece is ignored by the pipeline.

                Supported only for tabular and time series Datasets.
                This parameter must be used with training_fraction_split,
                validation_fraction_split, and test_fraction_split.
            model (~.model.Model):
                Optional. Describes the Model that may be uploaded (via
                [ModelService.UploadMode][]) by this TrainingPipeline. The
                TrainingPipeline's
                ``training_task_definition``
                should make clear whether this Model description should be
                populated, and if there are any special requirements
                regarding how it should be filled. If nothing is mentioned
                in the
                ``training_task_definition``,
                then it should be assumed that this field should not be
                filled and the training task either uploads the Model
                without a need of this information, or that training task
                does not support uploading a Model as part of the pipeline.
                When the Pipeline's state becomes
                ``PIPELINE_STATE_SUCCEEDED`` and the trained Model had been
                uploaded into Vertex AI, then the model_to_upload's
                resource ``name``
                is populated. The Model is always uploaded into the Project
                and Location in which this pipeline is.
            model_id (str):
                Optional. The ID to use for the Model produced by this job,
                which will become the final component of the model resource name.
                This value may be up to 63 characters, and valid characters
                are `[a-z0-9_-]`. The first character cannot be a number or hyphen.
            parent_model (str):
                Optional. The resource name or model ID of an existing model.
                The new model uploaded by this job will be a version of `parent_model`.

                Only set this field when training a new version of an existing model.
            is_default_version (bool):
                Optional. When set to True, the newly uploaded model version will
                automatically have alias "default" included. Subsequent uses of
                the model produced by this job without a version specified will
                use this "default" version.

                When set to False, the "default" alias will not be moved.
                Actions targeting the model version produced by this job will need
                to specifically reference this version by ID or alias.

                New model uploads, i.e. version 1, will always be "default" aliased.
            model_version_aliases (Sequence[str]):
                Optional. User provided version aliases so that the model version
                uploaded by this job can be referenced via alias instead of
                auto-generated version ID. A default version alias will be created
                for the first version of the model.

                The format is [a-z][a-zA-Z0-9-]{0,126}[a-z0-9]
            model_version_description (str):
               Optional. The description of the model version being uploaded by this job.
            gcs_destination_uri_prefix (str):
                Optional. The Google Cloud Storage location.

                The Vertex AI environment variables representing Google
                Cloud Storage data URIs will always be represented in the
                Google Cloud Storage wildcard format to support sharded
                data.

                -  AIP_DATA_FORMAT = "jsonl".
                -  AIP_TRAINING_DATA_URI = "gcs_destination/training-*"
                -  AIP_VALIDATION_DATA_URI = "gcs_destination/validation-*"
                -  AIP_TEST_DATA_URI = "gcs_destination/test-*".
            bigquery_destination (str):
                The BigQuery project location where the training data is to
                be written to. In the given project a new dataset is created
                with name
                ``dataset_<dataset-id>_<annotation-type>_<timestamp-of-training-call>``
                where timestamp is in YYYY_MM_DDThh_mm_ss_sssZ format. All
                training input data will be written into that dataset. In
                the dataset three tables will be created, ``training``,
                ``validation`` and ``test``.

                -  AIP_DATA_FORMAT = "bigquery".
                -  AIP_TRAINING_DATA_URI ="bigquery_destination.dataset_*.training"
                -  AIP_VALIDATION_DATA_URI = "bigquery_destination.dataset_*.validation"
                -  AIP_TEST_DATA_URI = "bigquery_destination.dataset_*.test"
            create_request_timeout (float):
                Optional. The timeout for the create request in seconds.
            block (bool):
                Optional. If True, block until complete.
        """

        input_data_config = self._create_input_data_config(
            dataset=dataset,
            annotation_schema_uri=annotation_schema_uri,
            training_fraction_split=training_fraction_split,
            validation_fraction_split=validation_fraction_split,
            test_fraction_split=test_fraction_split,
            training_filter_split=training_filter_split,
            validation_filter_split=validation_filter_split,
            test_filter_split=test_filter_split,
            predefined_split_column_name=predefined_split_column_name,
            timestamp_split_column_name=timestamp_split_column_name,
            gcs_destination_uri_prefix=gcs_destination_uri_prefix,
            bigquery_destination=bigquery_destination,
        )

        parent_model = models.ModelRegistry._get_true_version_parent(
            parent_model=parent_model,
            project=self.project,
            location=self.location,
        )

        if model:
            model.version_aliases = models.ModelRegistry._get_true_alias_list(
                version_aliases=model_version_aliases,
                is_default_version=is_default_version,
            )
            model.version_description = model_version_description

        # create training pipeline
        training_pipeline = gca_training_pipeline.TrainingPipeline(
            display_name=self._display_name,
            training_task_definition=training_task_definition,
            training_task_inputs=training_task_inputs,
            model_to_upload=model,
            model_id=model_id,
            parent_model=parent_model,
            input_data_config=input_data_config,
            labels=self._labels,
            encryption_spec=self._training_encryption_spec,
        )

        training_pipeline = self.api_client.create_training_pipeline(
            parent=initializer.global_config.common_location_path(
                self.project, self.location
            ),
            training_pipeline=training_pipeline,
            timeout=create_request_timeout,
        )

        self._gca_resource = training_pipeline

        _LOGGER.info("View Training:\n%s" % self._dashboard_uri())

        model = self._get_model(block=block)

        if model is None:
            _LOGGER.warning(
                "Training did not produce a Managed Model returning None. "
                + self._model_upload_fail_string
            )

        return model

    def _is_waiting_to_run(self) -> bool:
        """Returns `true` if the training job is pending upstream tasks,
        otherwise it returns `false`."""
        self._raise_future_exception()
        if self._latest_future:
            _LOGGER.info(
                "The training job is waiting for upstream SDK tasks to complete before"
                " launching."
            )
            return True
        return False

    @property
    def state(self) -> Optional[gca_pipeline_state.PipelineState]:
        """Current training state."""

        if self._assert_has_run():
            return

        self._sync_gca_resource()
        return self._gca_resource.state

    def get_model(self, sync=True) -> models.Model:
        """Returns the Vertex AI model produced by this training job.

        Args:
            sync (bool):
                If set to `true`, this method runs synchronously. If `false`, this
                method runs asynchronously.

        Returns:
            The Vertex AI model that was produced by this training job.

        Raises:
            RuntimeError: A runtime error is raised if the training job failed
              or if a model wasn't produced by the training job.
        """

        self._assert_has_run()
        if not self._gca_resource.model_to_upload:
            raise RuntimeError(self._model_upload_fail_string)

        return self._force_get_model(sync=sync)

    @base.optional_sync()
    def _force_get_model(self, sync: bool = True) -> models.Model:
        """Returns the Vertex AI model produced by this training job.

        Args:
            sync (bool):
                If set to `true`, this method runs synchronously. If `false`, this
                method runs asynchronously.

        Returns:
            The Vertex AI model that was produced by this training job.

        Raises:
            RuntimeError: If training failed or if a model was not produced by this training.
        """
        model = self._get_model()

        if model is None:
            raise RuntimeError(self._model_upload_fail_string)

        return model

    def _get_model(self, block: bool = True) -> Optional[models.Model]:
        """Helper method to get and instantiate the Model to Upload.

        Returns:
            model: Vertex AI Model if training succeeded and produced a Vertex AI
                Model. None otherwise.

        Raises:
            RuntimeError: If Training failed.
        """
        if block:
            self._block_until_complete()

        if self.has_failed:
            raise RuntimeError(
                f"Training Pipeline {self.resource_name} failed. No model available."
            )

        if not self._gca_resource.model_to_upload:
            return None

        if self._gca_resource.model_to_upload.name:
            return models.Model(
                model_name=self._gca_resource.model_to_upload.name,
                version=self._gca_resource.model_to_upload.version_id,
            )

    def _wait_callback(self):
        """Callback performs custom logging during _block_until_complete. Override in subclass."""
        pass

    def _block_until_complete(self):
        """Helper method to block and check on job until complete."""

        log_wait = _LOG_WAIT_TIME

        previous_time = time.time()

        while self.state not in _PIPELINE_COMPLETE_STATES:
            current_time = time.time()
            if current_time - previous_time >= log_wait:
                _LOGGER.info(
                    "%s %s current state:\n%s"
                    % (
                        self.__class__.__name__,
                        self._gca_resource.name,
                        self._gca_resource.state,
                    )
                )
                log_wait = min(log_wait * _WAIT_TIME_MULTIPLIER, _MAX_WAIT_TIME)
                previous_time = current_time
            self._wait_callback()
            time.sleep(_JOB_WAIT_TIME)

        self._raise_failure()

        _LOGGER.log_action_completed_against_resource("run", "completed", self)

        if self._gca_resource.model_to_upload and not self.has_failed:
            _LOGGER.info(
                "Model available at %s" % self._gca_resource.model_to_upload.name
            )

    def _raise_failure(self):
        """Helper method to raise failure if TrainingPipeline fails.

        Raises:
            RuntimeError: If training failed.
        """

        if self._gca_resource.error.code != code_pb2.OK:
            raise RuntimeError("Training failed with:\n%s" % self._gca_resource.error)

    @property
    def has_failed(self) -> bool:
        """Returns `true` if the training job failed, otherwise `false`."""
        self._assert_has_run()
        return self.state == gca_pipeline_state.PipelineState.PIPELINE_STATE_FAILED

    def _dashboard_uri(self) -> str:
        """Helper method to compose the dashboard uri where training can be
        viewed."""
        fields = self._parse_resource_name(self.resource_name)
        url = f"https://console.cloud.google.com/ai/platform/locations/{fields['location']}/training/{fields['training_pipeline']}?project={fields['project']}"
        return url

    @property
    def _has_run(self) -> bool:
        """Helper property to check if this training job has been run."""
        return self._gca_resource is not None

    def _assert_has_run(self) -> bool:
        """Helper method to assert that this training has run."""
        if not self._has_run:
            if self._is_waiting_to_run():
                return True
            raise RuntimeError(
                "TrainingPipeline has not been launched. You must run this"
                " TrainingPipeline using TrainingPipeline.run. "
            )
        return False

    @classmethod
    def list(
        cls,
        filter: Optional[str] = None,
        order_by: Optional[str] = None,
        project: Optional[str] = None,
        location: Optional[str] = None,
        credentials: Optional[auth_credentials.Credentials] = None,
    ) -> List["base.VertexAiResourceNoun"]:
        """Lists all instances of this training job resource.

        The following shows an example of how to call `CustomTrainingJob.list`:

        ```py
        aiplatform.CustomTrainingJob.list(
            filter='display_name="experiment_a27"',
            order_by='create_time desc'
        )
        ```

        Args:
            filter (str):
                Optional. An expression for filtering the results of the request.
                For field names, snake_case and camelCase are supported.
            order_by (str):
                Optional. A comma-separated list of fields used to sort the
                returned traing job resources. The defauilt sorting order is
                ascending. To sort by a field name in descending order, use
                `desc` after the field name. The following fields are supported:
                `display_name`, `create_time`, `update_time`.
            project (str):
                Optional. The name of the Google Cloud project to which to
                retrieve the list of training job resources. This overrides the
                project that was set by `aiplatform.init`.
            location (str):
                Optional. The Google Cloud region from where the training job
                resources are retrieved. This region overrides the region that
                was set by `aiplatform.init`.
            credentials (auth_credentials.Credentials):
                Optional. The credentials that are used to retrieve list. These
                credentials override the credentials set by `aiplatform.init`.

        Returns:
            List[VertexAiResourceNoun]: A list of training job resources.
        """

        training_job_subclass_filter = (
            lambda gapic_obj: gapic_obj.training_task_definition
            in cls._supported_training_schemas
        )

        return cls._list_with_local_order(
            cls_filter=training_job_subclass_filter,
            filter=filter,
            order_by=order_by,
            project=project,
            location=location,
            credentials=credentials,
        )

    def cancel(self) -> None:
        """Asynchronously attempts to cancel a training job.

        The server makes a best effort to cancel the job, but the training job
        can't always be cancelled. If the training job is canceled, its state
        transitions to `CANCELLED` and it's not deleted.

        Raises:
            RuntimeError: If this training job isn't running, then a runtime
              error is raised.
        """
        if not self._has_run:
            raise RuntimeError(
                "This TrainingJob has not been launched, use the `run()` method "
                "to start. `cancel()` can only be called on a job that is running."
            )
        self.api_client.cancel_training_pipeline(name=self.resource_name)

    def wait_for_resource_creation(self) -> None:
        """Waits until the resource has been created."""
        self._wait_for_resource_creation()


class _CustomTrainingJob(_TrainingJob):
    """ABC for Custom Training Pipelines.."""

    _supported_training_schemas = (schema.training_job.definition.custom_task,)

    def __init__(
        self,
        # TODO(b/223262536): Make display_name parameter fully optional in next major release
        display_name: str,
        container_uri: str,
        model_serving_container_image_uri: Optional[str] = None,
        model_serving_container_predict_route: Optional[str] = None,
        model_serving_container_health_route: Optional[str] = None,
        model_serving_container_command: Optional[Sequence[str]] = None,
        model_serving_container_args: Optional[Sequence[str]] = None,
        model_serving_container_environment_variables: Optional[Dict[str, str]] = None,
        model_serving_container_ports: Optional[Sequence[int]] = None,
        model_description: Optional[str] = None,
        model_instance_schema_uri: Optional[str] = None,
        model_parameters_schema_uri: Optional[str] = None,
        model_prediction_schema_uri: Optional[str] = None,
        explanation_metadata: Optional[explain.ExplanationMetadata] = None,
        explanation_parameters: Optional[explain.ExplanationParameters] = None,
        project: Optional[str] = None,
        location: Optional[str] = None,
        credentials: Optional[auth_credentials.Credentials] = None,
        labels: Optional[Dict[str, str]] = None,
        training_encryption_spec_key_name: Optional[str] = None,
        model_encryption_spec_key_name: Optional[str] = None,
        staging_bucket: Optional[str] = None,
    ):
        """
        Args:
            display_name (str):
                Required. The user-defined name of this TrainingPipeline.
            container_uri (str):
                Required: Uri of the training container image in the GCR.
            model_serving_container_image_uri (str):
                If the training produces a managed Vertex AI Model, the URI of the
                Model serving container suitable for serving the model produced by the
                training script.
            model_serving_container_predict_route (str):
                If the training produces a managed Vertex AI Model, An HTTP path to
                send prediction requests to the container, and which must be supported
                by it. If not specified a default HTTP path will be used by Vertex AI.
            model_serving_container_health_route (str):
                If the training produces a managed Vertex AI Model, an HTTP path to
                send health check requests to the container, and which must be supported
                by it. If not specified a standard HTTP path will be used by AI
                Platform.
            model_serving_container_command (Sequence[str]):
                The command with which the container is run. Not executed within a
                shell. The Docker image's ENTRYPOINT is used if this is not provided.
                Variable references $(VAR_NAME) are expanded using the container's
                environment. If a variable cannot be resolved, the reference in the
                input string will be unchanged. The $(VAR_NAME) syntax can be escaped
                with a double $$, ie: $$(VAR_NAME). Escaped references will never be
                expanded, regardless of whether the variable exists or not.
            model_serving_container_args (Sequence[str]):
                The arguments to the command. The Docker image's CMD is used if this is
                not provided. Variable references $(VAR_NAME) are expanded using the
                container's environment. If a variable cannot be resolved, the reference
                in the input string will be unchanged. The $(VAR_NAME) syntax can be
                escaped with a double $$, ie: $$(VAR_NAME). Escaped references will
                never be expanded, regardless of whether the variable exists or not.
            model_serving_container_environment_variables (Dict[str, str]):
                The environment variables that are to be present in the container.
                Should be a dictionary where keys are environment variable names
                and values are environment variable values for those names.
            model_serving_container_ports (Sequence[int]):
                Declaration of ports that are exposed by the container. This field is
                primarily informational, it gives Vertex AI information about the
                network connections the container uses. Listing or not a port here has
                no impact on whether the port is actually exposed, any port listening on
                the default "0.0.0.0" address inside a container will be accessible from
                the network.
            model_description (str):
                The description of the Model.
            model_instance_schema_uri (str):
                Optional. Points to a YAML file stored on Google Cloud
                Storage describing the format of a single instance, which
                are used in
                ``PredictRequest.instances``,
                ``ExplainRequest.instances``
                and
                ``BatchPredictionJob.input_config``.
                The schema is defined as an OpenAPI 3.0.2 `Schema
                Object <https://tinyurl.com/y538mdwt#schema-object>`__.
                AutoML Models always have this field populated by AI
                Platform. Note: The URI given on output will be immutable
                and probably different, including the URI scheme, than the
                one given on input. The output URI will point to a location
                where the user only has a read access.
            model_parameters_schema_uri (str):
                Optional. Points to a YAML file stored on Google Cloud
                Storage describing the parameters of prediction and
                explanation via
                ``PredictRequest.parameters``,
                ``ExplainRequest.parameters``
                and
                ``BatchPredictionJob.model_parameters``.
                The schema is defined as an OpenAPI 3.0.2 `Schema
                Object <https://tinyurl.com/y538mdwt#schema-object>`__.
                AutoML Models always have this field populated by AI
                Platform, if no parameters are supported it is set to an
                empty string. Note: The URI given on output will be
                immutable and probably different, including the URI scheme,
                than the one given on input. The output URI will point to a
                location where the user only has a read access.
            model_prediction_schema_uri (str):
                Optional. Points to a YAML file stored on Google Cloud
                Storage describing the format of a single prediction
                produced by this Model, which are returned via
                ``PredictResponse.predictions``,
                ``ExplainResponse.explanations``,
                and
                ``BatchPredictionJob.output_config``.
                The schema is defined as an OpenAPI 3.0.2 `Schema
                Object <https://tinyurl.com/y538mdwt#schema-object>`__.
                AutoML Models always have this field populated by AI
                Platform. Note: The URI given on output will be immutable
                and probably different, including the URI scheme, than the
                one given on input. The output URI will point to a location
                where the user only has a read access.
            explanation_metadata (explain.ExplanationMetadata):
                Optional. Metadata describing the Model's input and output for
                explanation. `explanation_metadata` is optional while
                `explanation_parameters` must be specified when used.
                For more details, see `Ref docs <http://tinyurl.com/1igh60kt>`
            explanation_parameters (explain.ExplanationParameters):
                Optional. Parameters to configure explaining for Model's
                predictions.
                For more details, see `Ref docs <http://tinyurl.com/1an4zake>`
            project (str):
                Project to run training in. Overrides project set in aiplatform.init.
            location (str):
                Location to run training in. Overrides location set in aiplatform.init.
            credentials (auth_credentials.Credentials):
                Custom credentials to use to run call training service. Overrides
                credentials set in aiplatform.init.
            labels (Dict[str, str]):
                Optional. The labels with user-defined metadata to
                organize TrainingPipelines.
                Label keys and values can be no longer than 64
                characters (Unicode codepoints), can only
                contain lowercase letters, numeric characters,
                underscores and dashes. International characters
                are allowed.
                See https://goo.gl/xmQnxf for more information
                and examples of labels.
            training_encryption_spec_key_name (Optional[str]):
                Optional. The Cloud KMS resource identifier of the customer
                managed encryption key used to protect the training pipeline. Has the
                form:
                ``projects/my-project/locations/my-region/keyRings/my-kr/cryptoKeys/my-key``.
                The key needs to be in the same region as where the compute
                resource is created.

                If set, this TrainingPipeline will be secured by this key.

                Note: Model trained by this TrainingPipeline is also secured
                by this key if ``model_to_upload`` is not set separately.

                Overrides encryption_spec_key_name set in aiplatform.init.
            model_encryption_spec_key_name (Optional[str]):
                Optional. The Cloud KMS resource identifier of the customer
                managed encryption key used to protect the model. Has the
                form:
                ``projects/my-project/locations/my-region/keyRings/my-kr/cryptoKeys/my-key``.
                The key needs to be in the same region as where the compute
                resource is created.

                If set, the trained Model will be secured by this key.

                Overrides encryption_spec_key_name set in aiplatform.init.
            staging_bucket (str):
                Bucket used to stage source and training artifacts. Overrides
                staging_bucket set in aiplatform.init.
        """
        if not display_name:
            display_name = self.__class__._generate_display_name()
        super().__init__(
            display_name=display_name,
            project=project,
            location=location,
            credentials=credentials,
            labels=labels,
            training_encryption_spec_key_name=training_encryption_spec_key_name,
            model_encryption_spec_key_name=model_encryption_spec_key_name,
        )

        self._container_uri = container_uri

        model_predict_schemata = None
        if any(
            [
                model_instance_schema_uri,
                model_parameters_schema_uri,
                model_prediction_schema_uri,
            ]
        ):
            model_predict_schemata = gca_model.PredictSchemata(
                instance_schema_uri=model_instance_schema_uri,
                parameters_schema_uri=model_parameters_schema_uri,
                prediction_schema_uri=model_prediction_schema_uri,
            )

        # Create the container spec
        env = None
        ports = None

        if model_serving_container_environment_variables:
            env = [
                gca_env_var.EnvVar(name=str(key), value=str(value))
                for key, value in model_serving_container_environment_variables.items()
            ]

        if model_serving_container_ports:
            ports = [
                gca_model.Port(container_port=port)
                for port in model_serving_container_ports
            ]

        container_spec = gca_model.ModelContainerSpec(
            image_uri=model_serving_container_image_uri,
            command=model_serving_container_command,
            args=model_serving_container_args,
            env=env,
            ports=ports,
            predict_route=model_serving_container_predict_route,
            health_route=model_serving_container_health_route,
        )

        # create model payload
        self._managed_model = gca_model.Model(
            description=model_description,
            predict_schemata=model_predict_schemata,
            container_spec=container_spec,
            encryption_spec=self._model_encryption_spec,
        )

        self._staging_bucket = (
            staging_bucket or initializer.global_config.staging_bucket
        )

        if not self._staging_bucket:
            raise RuntimeError(
                "staging_bucket should be set in TrainingJob constructor or "
                "set using aiplatform.init(staging_bucket='gs://my-bucket')"
            )

        # Save explanationSpec as instance attributes
        self._explanation_metadata = explanation_metadata
        self._explanation_parameters = explanation_parameters

        # Backing Custom Job resource is not known until after data preprocessing
        # once Custom Job is known we log the console uri and the tensorboard uri
        # this flags keeps that state so we don't log it multiple times
        self._has_logged_custom_job = False
        self._logged_web_access_uris = set()

    @property
    def network(self) -> Optional[str]:
        """The full name of the Google Compute Engine
        [network](https://cloud.google.com/vpc/docs/vpc#networks) to which this
        `CustomTrainingJob` should be peered.

        Specify the name of the network using the format
        `projects/{project}/global/networks/{network}`. Replace {project} with
        the project number, such as `12345`, and {network} with a network name.

        Before specifying a network, private services access must be configured
        for the network. If private services access isn't configured, then
        the custom training job can't be peered with a network.
        """
        # Return `network` value in training task inputs if set in Map
        self._assert_gca_resource_is_available()
        return self._gca_resource.training_task_inputs.get("network")

    def _prepare_and_validate_run(
        self,
        model_display_name: Optional[str] = None,
        model_labels: Optional[Dict[str, str]] = None,
        replica_count: int = 1,
        machine_type: str = "n1-standard-4",
        accelerator_type: str = "ACCELERATOR_TYPE_UNSPECIFIED",
        accelerator_count: int = 0,
        boot_disk_type: str = "pd-ssd",
        boot_disk_size_gb: int = 100,
        reduction_server_replica_count: int = 0,
        reduction_server_machine_type: Optional[str] = None,
        tpu_topology: Optional[str] = None,
    ) -> Tuple[worker_spec_utils._DistributedTrainingSpec, Optional[gca_model.Model]]:
        """Create worker pool specs and managed model as well validating the
        run.

        Args:
            model_display_name (str):
                If the script produces a managed Vertex AI Model. The display name of
                the Model. The name can be up to 128 characters long and can be consist
                of any UTF-8 characters.

                If not provided upon creation, the job's display_name is used.
            model_labels (Dict[str, str]):
                Optional. The labels with user-defined metadata to
                organize your Models.
                Label keys and values can be no longer than 64
                characters (Unicode codepoints), can only
                contain lowercase letters, numeric characters,
                underscores and dashes. International characters
                are allowed.
                See https://goo.gl/xmQnxf for more information
                and examples of labels.
            replica_count (int):
                The number of worker replicas. If replica count = 1 then one chief
                replica will be provisioned. If replica_count > 1 the remainder will be
                provisioned as a worker replica pool.
            machine_type (str):
                The type of machine to use for training.
            accelerator_type (str):
                Hardware accelerator type. One of ACCELERATOR_TYPE_UNSPECIFIED,
                NVIDIA_TESLA_K80, NVIDIA_TESLA_P100, NVIDIA_TESLA_V100, NVIDIA_TESLA_P4,
                NVIDIA_TESLA_T4
            accelerator_count (int):
                The number of accelerators to attach to a worker replica.
            boot_disk_type (str):
                Type of the boot disk, default is `pd-ssd`.
                Valid values: `pd-ssd` (Persistent Disk Solid State Drive) or
                `pd-standard` (Persistent Disk Hard Disk Drive).
            boot_disk_size_gb (int):
                Size in GB of the boot disk, default is 100GB.
                boot disk size must be within the range of [100, 64000].
            reduction_server_replica_count (int):
                The number of reduction server replicas, default is 0.
            reduction_server_machine_type (str):
                Optional. The type of machine to use for reduction server.
            tpu_topology (str):
                Optional. Only required if the machine type is a TPU
                v5 version.

        Returns:
            Worker pools specs and managed model for run.

        Raises:
            RuntimeError: If Training job has already been run or model_display_name was
                provided but required arguments were not provided in constructor.
        """

        if self._is_waiting_to_run():
            raise RuntimeError("Custom Training is already scheduled to run.")

        if self._has_run:
            raise RuntimeError("Custom Training has already run.")

        # if args needed for model is incomplete
        if model_display_name and not self._managed_model.container_spec.image_uri:
            raise RuntimeError(
                """model_display_name was provided but
                model_serving_container_image_uri was not provided when this
                custom pipeline was constructed.
                """
            )

        if self._managed_model.container_spec.image_uri:
            model_display_name = model_display_name or self._display_name + "-model"

        # validates args and will raise
        worker_pool_specs = (
            worker_spec_utils._DistributedTrainingSpec.chief_worker_pool(
                replica_count=replica_count,
                machine_type=machine_type,
                accelerator_count=accelerator_count,
                accelerator_type=accelerator_type,
                boot_disk_type=boot_disk_type,
                boot_disk_size_gb=boot_disk_size_gb,
                reduction_server_replica_count=reduction_server_replica_count,
                reduction_server_machine_type=reduction_server_machine_type,
                tpu_topology=tpu_topology,
            ).pool_specs
        )

        managed_model = self._managed_model
        if model_display_name:
            utils.validate_display_name(model_display_name)
            managed_model.display_name = model_display_name
            if model_labels:
                utils.validate_labels(model_labels)
                managed_model.labels = model_labels
            else:
                managed_model.labels = self._labels
            managed_model.explanation_spec = (
                _explanation_utils.create_and_validate_explanation_spec(
                    explanation_metadata=self._explanation_metadata,
                    explanation_parameters=self._explanation_parameters,
                )
            )
        else:
            managed_model = None

        return worker_pool_specs, managed_model

    def _prepare_training_task_inputs_and_output_dir(
        self,
        worker_pool_specs: worker_spec_utils._DistributedTrainingSpec,
        base_output_dir: Optional[str] = None,
        service_account: Optional[str] = None,
        network: Optional[str] = None,
        timeout: Optional[int] = None,
        restart_job_on_worker_restart: bool = False,
        enable_web_access: bool = False,
        enable_dashboard_access: bool = False,
        tensorboard: Optional[str] = None,
        disable_retries: bool = False,
        persistent_resource_id: Optional[str] = None,
    ) -> Tuple[Dict, str]:
        """Prepares training task inputs and output directory for custom job.

        Args:
            worker_pools_spec (worker_spec_utils._DistributedTrainingSpec):
                Worker pools pecs required to run job.
            base_output_dir (str):
                GCS output directory of job. If not provided a
                timestamped directory in the staging directory will be used.
            service_account (str):
                Specifies the service account for workload run-as account.
                Users submitting jobs must have act-as permission on this run-as account.
            network (str):
                The full name of the Compute Engine network to which the job
                should be peered. For example, projects/12345/global/networks/myVPC.
                Private services access must already be configured for the network.
                If left unspecified, the job is not peered with any network.
            timeout (int):
                The maximum job running time in seconds. The default is 7 days.
            restart_job_on_worker_restart (bool):
                Restarts the entire CustomJob if a worker
                gets restarted. This feature can be used by
                distributed training jobs that are not resilient
                to workers leaving and joining a job.
            enable_web_access (bool):
                Whether you want Vertex AI to enable interactive shell access
                to training containers.
                https://cloud.google.com/vertex-ai/docs/training/monitor-debug-interactive-shell
            enable_dashboard_access (bool):
                Whether you want Vertex AI to enable access to the customized dashboard
                to training containers.
            tensorboard (str):
                Optional. The name of a Vertex AI
                [Tensorboard][google.cloud.aiplatform.v1beta1.Tensorboard]
                resource to which this CustomJob will upload Tensorboard
                logs. Format:
                ``projects/{project}/locations/{location}/tensorboards/{tensorboard}``

                The training script should write Tensorboard to following Vertex AI environment
                variable:

                AIP_TENSORBOARD_LOG_DIR

                `service_account` is required with provided `tensorboard`.
                For more information on configuring your service account please visit:
                https://cloud.google.com/vertex-ai/docs/experiments/tensorboard-training
            disable_retries (bool):
                Indicates if the job should retry for internal errors after the
                job starts running. If True, overrides
                `restart_job_on_worker_restart` to False.
            persistent_resource_id (str):
                Optional. The ID of the PersistentResource in the same Project
                and Location. If this is specified, the job will be run on
                existing machines held by the PersistentResource instead of
                on-demand short-live machines. The network, CMEK, and node pool
                configs on the job should be consistent with those on the
                PersistentResource, otherwise, the job will be rejected.

        Returns:
            Training task inputs and Output directory for custom job.
        """

        # default directory if not given
        base_output_dir = base_output_dir or _timestamped_gcs_dir(
            self._staging_bucket, "aiplatform-custom-training"
        )

        _LOGGER.info("Training Output directory:\n%s " % base_output_dir)

        training_task_inputs = {
            "worker_pool_specs": worker_pool_specs,
            "base_output_directory": {"output_uri_prefix": base_output_dir},
        }

        if service_account:
            training_task_inputs["service_account"] = service_account
        if network:
            training_task_inputs["network"] = network
        if tensorboard:
            training_task_inputs["tensorboard"] = tensorboard
        if enable_web_access:
            training_task_inputs["enable_web_access"] = enable_web_access
        if enable_dashboard_access:
            training_task_inputs["enable_dashboard_access"] = enable_dashboard_access
        if persistent_resource_id:
            training_task_inputs["persistent_resource_id"] = persistent_resource_id

        if timeout or restart_job_on_worker_restart or disable_retries:
            timeout = f"{timeout}s" if timeout else None
            scheduling = {
                "timeout": timeout,
                "restart_job_on_worker_restart": restart_job_on_worker_restart,
                "disable_retries": disable_retries,
            }
            training_task_inputs["scheduling"] = scheduling

        return training_task_inputs, base_output_dir

    @property
    def web_access_uris(self) -> Dict[str, str]:
        """Returns the URIs used to access the custom training job."""
        web_access_uris = dict()
        if (
            self._gca_resource.training_task_metadata
            and self._gca_resource.training_task_metadata.get("backingCustomJob")
        ):
            custom_job_resource_name = self._gca_resource.training_task_metadata.get(
                "backingCustomJob"
            )
            custom_job = jobs.CustomJob.get(resource_name=custom_job_resource_name)

            web_access_uris = dict(custom_job.web_access_uris)

        return web_access_uris

    def _log_web_access_uris(self):
        """Helper method to log the web access uris of the backing custom job"""
        for worker, uri in self.web_access_uris.items():
            if uri not in self._logged_web_access_uris:
                _LOGGER.info(
                    "%s %s access the interactive shell terminals for the backing custom job:\n%s:\n%s"
                    % (
                        self.__class__.__name__,
                        self._gca_resource.name,
                        worker,
                        uri,
                    ),
                )
                self._logged_web_access_uris.add(uri)

    def _wait_callback(self):
        if (
            self._gca_resource.training_task_metadata
            and self._gca_resource.training_task_metadata.get("backingCustomJob")
            and not self._has_logged_custom_job
        ):
            _LOGGER.info(f"View backing custom job:\n{self._custom_job_console_uri()}")

            if self._gca_resource.training_task_inputs.get("tensorboard"):
                _LOGGER.info(f"View tensorboard:\n{self._tensorboard_console_uri()}")

            self._has_logged_custom_job = True

        if self._gca_resource.training_task_inputs.get(
            "enable_web_access"
        ) or self._gca_resource.training_task_inputs.get("enable_dashboard_access"):
            self._log_web_access_uris()

    def _custom_job_console_uri(self) -> str:
        """Helper method to compose the dashboard uri where custom job can be viewed."""
        custom_job_resource_name = self._gca_resource.training_task_metadata.get(
            "backingCustomJob"
        )
        return console_utils.custom_job_console_uri(custom_job_resource_name)

    def _tensorboard_console_uri(self) -> str:
        """Helper method to compose dashboard uri where tensorboard can be viewed."""
        tensorboard_resource_name = self._gca_resource.training_task_inputs.get(
            "tensorboard"
        )
        custom_job_resource_name = self._gca_resource.training_task_metadata.get(
            "backingCustomJob"
        )
        return console_utils.custom_job_tensorboard_console_uri(
            tensorboard_resource_name, custom_job_resource_name
        )

    @property
    def _model_upload_fail_string(self) -> str:
        """Helper property for model upload failure."""
        return (
            f"Training Pipeline {self.resource_name} is not configured to upload a "
            "Model. Create the Training Pipeline with "
            "model_serving_container_image_uri and model_display_name passed in. "
            "Ensure that your training script saves to model to "
            "os.environ['AIP_MODEL_DIR']."
        )


class _ForecastingTrainingJob(_TrainingJob):
    """ABC for Forecasting Training Pipelines."""

    _supported_training_schemas = tuple()

    def __init__(
        self,
        display_name: Optional[str] = None,
        optimization_objective: Optional[str] = None,
        column_specs: Optional[Dict[str, str]] = None,
        column_transformations: Optional[List[Dict[str, Dict[str, str]]]] = None,
        project: Optional[str] = None,
        location: Optional[str] = None,
        credentials: Optional[auth_credentials.Credentials] = None,
        labels: Optional[Dict[str, str]] = None,
        training_encryption_spec_key_name: Optional[str] = None,
        model_encryption_spec_key_name: Optional[str] = None,
    ):
        """Constructs a Forecasting Training Job.

        Args:
            display_name (str):
                Optional. The user-defined name of this TrainingPipeline.
            optimization_objective (str):
                Optional. Objective function the model is to be optimized towards.
                The training process creates a Model that optimizes the value of the objective
                function over the validation set. The supported optimization objectives:
                "minimize-rmse" (default) - Minimize root-mean-squared error (RMSE).
                "minimize-mae" - Minimize mean-absolute error (MAE).
                "minimize-rmsle" - Minimize root-mean-squared log error (RMSLE).
                "minimize-rmspe" - Minimize root-mean-squared percentage error (RMSPE).
                "minimize-wape-mae" - Minimize the combination of weighted absolute percentage error (WAPE)
                                      and mean-absolute-error (MAE).
                "minimize-quantile-loss" - Minimize the quantile loss at the defined quantiles.
                                           (Set this objective to build quantile forecasts.)
            column_specs (Dict[str, str]):
                Optional. Alternative to column_transformations where the keys of the dict
                are column names and their respective values are one of
                AutoMLTabularTrainingJob.column_data_types.
                When creating transformation for BigQuery Struct column, the column
                should be flattened using "." as the delimiter. Only columns with no child
                should have a transformation.
                If an input column has no transformations on it, such a column is
                ignored by the training, except for the targetColumn, which should have
                no transformations defined on.
                Only one of column_transformations or column_specs should be passed.
            column_transformations (List[Dict[str, Dict[str, str]]]):
                Optional. Transformations to apply to the input columns (i.e. columns other
                than the targetColumn). Each transformation may produce multiple
                result values from the column's value, and all are used for training.
                When creating transformation for BigQuery Struct column, the column
                should be flattened using "." as the delimiter. Only columns with no child
                should have a transformation.
                If an input column has no transformations on it, such a column is
                ignored by the training, except for the targetColumn, which should have
                no transformations defined on.
                Only one of column_transformations or column_specs should be passed.
                Consider using column_specs as column_transformations will be deprecated eventually.
            project (str):
                Optional. Project to run training in. Overrides project set in aiplatform.init.
            location (str):
                Optional. Location to run training in. Overrides location set in aiplatform.init.
            credentials (auth_credentials.Credentials):
                Optional. Custom credentials to use to run call training service. Overrides
                credentials set in aiplatform.init.
            labels (Dict[str, str]):
                Optional. The labels with user-defined metadata to
                organize TrainingPipelines.
                Label keys and values can be no longer than 64
                characters (Unicode codepoints), can only
                contain lowercase letters, numeric characters,
                underscores and dashes. International characters
                are allowed.
                See https://goo.gl/xmQnxf for more information
                and examples of labels.
            training_encryption_spec_key_name (Optional[str]):
                Optional. The Cloud KMS resource identifier of the customer
                managed encryption key used to protect the training pipeline. Has the
                form:
                ``projects/my-project/locations/my-region/keyRings/my-kr/cryptoKeys/my-key``.
                The key needs to be in the same region as where the compute
                resource is created.
                If set, this TrainingPipeline will be secured by this key.
                Note: Model trained by this TrainingPipeline is also secured
                by this key if ``model_to_upload`` is not set separately.
                Overrides encryption_spec_key_name set in aiplatform.init.
            model_encryption_spec_key_name (Optional[str]):
                Optional. The Cloud KMS resource identifier of the customer
                managed encryption key used to protect the model. Has the
                form:
                ``projects/my-project/locations/my-region/keyRings/my-kr/cryptoKeys/my-key``.
                The key needs to be in the same region as where the compute
                resource is created.
                If set, the trained Model will be secured by this key.
                Overrides encryption_spec_key_name set in aiplatform.init.
        Raises:
            ValueError: If both column_transformations and column_specs were provided.
        """
        super().__init__(
            display_name=display_name,
            project=project,
            location=location,
            credentials=credentials,
            labels=labels,
            training_encryption_spec_key_name=training_encryption_spec_key_name,
            model_encryption_spec_key_name=model_encryption_spec_key_name,
        )

        self._column_transformations = (
            column_transformations_utils.validate_and_get_column_transformations(
                column_specs,
                column_transformations,
            )
        )

        self._optimization_objective = optimization_objective
        self._additional_experiments = []

    @property
    @classmethod
    @abc.abstractmethod
    def _model_type(cls) -> str:
        """The type of forecasting model."""
        pass

    @property
    @classmethod
    @abc.abstractmethod
    def _training_task_definition(cls) -> str:
        """A GCS path to the YAML file that defines the training task.

        The definition files that can be used here are found in
        gs://google-cloud-aiplatform/schema/trainingjob/definition/.
        """
        pass

    def run(
        self,
        dataset: datasets.TimeSeriesDataset,
        target_column: str,
        time_column: str,
        time_series_identifier_column: str,
        unavailable_at_forecast_columns: List[str],
        available_at_forecast_columns: List[str],
        forecast_horizon: int,
        data_granularity_unit: str,
        data_granularity_count: int,
        training_fraction_split: Optional[float] = None,
        validation_fraction_split: Optional[float] = None,
        test_fraction_split: Optional[float] = None,
        predefined_split_column_name: Optional[str] = None,
        timestamp_split_column_name: Optional[str] = None,
        weight_column: Optional[str] = None,
        time_series_attribute_columns: Optional[List[str]] = None,
        context_window: Optional[int] = None,
        export_evaluated_data_items: bool = False,
        export_evaluated_data_items_bigquery_destination_uri: Optional[str] = None,
        export_evaluated_data_items_override_destination: bool = False,
        quantiles: Optional[List[float]] = None,
        validation_options: Optional[str] = None,
        budget_milli_node_hours: int = 1000,
        model_display_name: Optional[str] = None,
        model_labels: Optional[Dict[str, str]] = None,
        model_id: Optional[str] = None,
        parent_model: Optional[str] = None,
        is_default_version: Optional[bool] = True,
        model_version_aliases: Optional[Sequence[str]] = None,
        model_version_description: Optional[str] = None,
        additional_experiments: Optional[List[str]] = None,
        hierarchy_group_columns: Optional[List[str]] = None,
        hierarchy_group_total_weight: Optional[float] = None,
        hierarchy_temporal_total_weight: Optional[float] = None,
        hierarchy_group_temporal_total_weight: Optional[float] = None,
        window_column: Optional[str] = None,
        window_stride_length: Optional[int] = None,
        window_max_count: Optional[int] = None,
        holiday_regions: Optional[List[str]] = None,
        sync: bool = True,
        create_request_timeout: Optional[float] = None,
        enable_probabilistic_inference: bool = False,
    ) -> models.Model:
        """Runs the training job and returns a model.

        If training on a Vertex AI dataset, you can use one of the following split configurations:
            Data fraction splits:
            Any of ``training_fraction_split``, ``validation_fraction_split`` and
            ``test_fraction_split`` may optionally be provided, they must sum to up to 1. If
            the provided ones sum to less than 1, the remainder is assigned to sets as
            decided by Vertex AI. If none of the fractions are set, by default roughly 80%
            of data will be used for training, 10% for validation, and 10% for test.

            Predefined splits:
            Assigns input data to training, validation, and test sets based on the value of a provided key.
            If using predefined splits, ``predefined_split_column_name`` must be provided.
            Supported only for tabular Datasets.

            Timestamp splits:
            Assigns input data to training, validation, and test sets
            based on a provided timestamps. The youngest data pieces are
            assigned to training set, next to validation set, and the oldest
            to the test set.
            Supported only for tabular Datasets.

        Args:
            dataset (datasets.TimeSeriesDataset):
                Required. The dataset within the same Project from which data will be used to train the Model. The
                Dataset must use schema compatible with Model being trained,
                and what is compatible should be described in the used
                TrainingPipeline's [training_task_definition]
                [google.cloud.aiplatform.v1beta1.TrainingPipeline.training_task_definition].
                For time series Datasets, all their data is exported to
                training, to pick and choose from.
            target_column (str):
                Required. Name of the column that the Model is to predict values for. This
                column must be unavailable at forecast.
            time_column (str):
                Required. Name of the column that identifies time order in the time series.
                This column must be available at forecast.
            time_series_identifier_column (str):
                Required. Name of the column that identifies the time series.
            unavailable_at_forecast_columns (List[str]):
                Required. Column names of columns that are unavailable at forecast.
                Each column contains information for the given entity (identified by the
                [time_series_identifier_column]) that is unknown before the forecast
                (e.g. population of a city in a given year, or weather on a given day).
            available_at_forecast_columns (List[str]):
                Required. Column names of columns that are available at forecast.
                Each column contains information for the given entity (identified by the
                [time_series_identifier_column]) that is known at forecast.
            forecast_horizon: (int):
                Required. The amount of time into the future for which forecasted values for the target are
                returned. Expressed in number of units defined by the [data_granularity_unit] and
                [data_granularity_count] field. Inclusive.
            data_granularity_unit (str):
                Required. The data granularity unit. Accepted values are ``minute``,
                ``hour``, ``day``, ``week``, ``month``, ``year``.
            data_granularity_count (int):
                Required. The number of data granularity units between data points in the training
                data. If [data_granularity_unit] is `minute`, can be 1, 5, 10, 15, or 30. For all other
                values of [data_granularity_unit], must be 1.
            predefined_split_column_name (str):
                Optional. The key is a name of one of the Dataset's data
                columns. The value of the key (either the label's value or
                value in the column) must be one of {``TRAIN``,
                ``VALIDATE``, ``TEST``}, and it defines to which set the
                given piece of data is assigned. If for a piece of data the
                key is not present or has an invalid value, that piece is
                ignored by the pipeline.

                Supported only for tabular and time series Datasets.
            timestamp_split_column_name (str):
                Optional. The key is a name of one of the Dataset's data
                columns. The value of the key values of the key (the values in
                the column) must be in RFC 3339 `date-time` format, where
                `time-offset` = `"Z"` (e.g. 1985-04-12T23:20:50.52Z). If for a
                piece of data the key is not present or has an invalid value,
                that piece is ignored by the pipeline.
                Supported only for tabular and time series Datasets.
                This parameter must be used with training_fraction_split,
                validation_fraction_split, and test_fraction_split.
            weight_column (str):
                Optional. Name of the column that should be used as the weight column.
                Higher values in this column give more importance to the row
                during Model training. The column must have numeric values between 0 and
                10000 inclusively, and 0 value means that the row is ignored.
                If the weight column field is not set, then all rows are assumed to have
                equal weight of 1.
            time_series_attribute_columns (List[str]):
                Optional. Column names that should be used as attribute columns.
                Each column is constant within a time series.
            context_window (int):
                Optional. The amount of time into the past training and prediction data is used for
                model training and prediction respectively. Expressed in number of units defined by the
                [data_granularity_unit] and [data_granularity_count] fields. When not provided uses the
                default value of 0 which means the model sets each series context window to be 0 (also
                known as "cold start"). Inclusive.
            export_evaluated_data_items (bool):
                Whether to export the test set predictions to a BigQuery table.
                If False, then the export is not performed.
            export_evaluated_data_items_bigquery_destination_uri (string):
                Optional. URI of desired destination BigQuery table for exported test set predictions.

                Expected format:
                ``bq://<project_id>:<dataset_id>:<table>``

                If not specified, then results are exported to the following auto-created BigQuery
                table:
                ``<project_id>:export_evaluated_examples_<model_name>_<yyyy_MM_dd'T'HH_mm_ss_SSS'Z'>.evaluated_examples``

                Applies only if [export_evaluated_data_items] is True.
            export_evaluated_data_items_override_destination (bool):
                Whether to override the contents of [export_evaluated_data_items_bigquery_destination_uri],
                if the table exists, for exported test set predictions. If False, and the
                table exists, then the training job will fail.

                Applies only if [export_evaluated_data_items] is True and
                [export_evaluated_data_items_bigquery_destination_uri] is specified.
            quantiles (List[float]):
                Quantiles to use for the `minimize-quantile-loss`
                [AutoMLForecastingTrainingJob.optimization_objective]. This argument is required in
                this case.

                Accepts up to 5 quantiles in the form of a double from 0 to 1, exclusive.
                Each quantile must be unique.
            validation_options (str):
                Validation options for the data validation component. The available options are:
                "fail-pipeline" - (default), will validate against the validation and fail the pipeline
                                  if it fails.
                "ignore-validation" - ignore the results of the validation and continue the pipeline
            budget_milli_node_hours (int):
                Optional. The train budget of creating this Model, expressed in milli node
                hours i.e. 1,000 value in this field means 1 node hour.
                The training cost of the model will not exceed this budget. The final
                cost will be attempted to be close to the budget, though may end up
                being (even) noticeably smaller - at the backend's discretion. This
                especially may happen when further model training ceases to provide
                any improvements.
                If the budget is set to a value known to be insufficient to train a
                Model for the given training set, the training won't be attempted and
                will error.
                The minimum value is 1000 and the maximum is 72000.
            model_display_name (str):
                Optional. If the script produces a managed Vertex AI Model. The display name of
                the Model. The name can be up to 128 characters long and can be consist
                of any UTF-8 characters.

                If not provided upon creation, the job's display_name is used.
            model_labels (Dict[str, str]):
                Optional. The labels with user-defined metadata to
                organize your Models.
                Label keys and values can be no longer than 64
                characters (Unicode codepoints), can only
                contain lowercase letters, numeric characters,
                underscores and dashes. International characters
                are allowed.
                See https://goo.gl/xmQnxf for more information
                and examples of labels.
            model_id (str):
                Optional. The ID to use for the Model produced by this job,
                which will become the final component of the model resource name.
                This value may be up to 63 characters, and valid characters
                are `[a-z0-9_-]`. The first character cannot be a number or hyphen.
            parent_model (str):
                Optional. The resource name or model ID of an existing model.
                The new model uploaded by this job will be a version of `parent_model`.

                Only set this field when training a new version of an existing model.
            is_default_version (bool):
                Optional. When set to True, the newly uploaded model version will
                automatically have alias "default" included. Subsequent uses of
                the model produced by this job without a version specified will
                use this "default" version.

                When set to False, the "default" alias will not be moved.
                Actions targeting the model version produced by this job will need
                to specifically reference this version by ID or alias.

                New model uploads, i.e. version 1, will always be "default" aliased.
            model_version_aliases (Sequence[str]):
                Optional. User provided version aliases so that the model version
                uploaded by this job can be referenced via alias instead of
                auto-generated version ID. A default version alias will be created
                for the first version of the model.

                The format is [a-z][a-zA-Z0-9-]{0,126}[a-z0-9]
            model_version_description (str):
               Optional. The description of the model version being uploaded by this job.
            additional_experiments (List[str]):
                Optional. Additional experiment flags for the time series forcasting training.
            create_request_timeout (float):
                Optional. The timeout for the create request in seconds.
            hierarchy_group_columns (List[str]):
                Optional. A list of time series attribute column names that
                define the time series hierarchy. Only one level of hierarchy is
                supported, ex. ``region`` for a hierarchy of stores or
                ``department`` for a hierarchy of products. If multiple columns
                are specified, time series will be grouped by their combined
                values, ex. (``blue``, ``large``) for ``color`` and ``size``, up
                to 5 columns are accepted. If no group columns are specified,
                all time series are considered to be part of the same group.
            hierarchy_group_total_weight (float):
                Optional. The weight of the loss for predictions aggregated over
                time series in the same hierarchy group.
            hierarchy_temporal_total_weight (float):
                Optional. The weight of the loss for predictions aggregated over
                the horizon for a single time series.
            hierarchy_group_temporal_total_weight (float):
                Optional. The weight of the loss for predictions aggregated over
                both the horizon and time series in the same hierarchy group.
            window_column (str):
                Optional. Name of the column that should be used to filter input
                rows. The column should contain either booleans or string
                booleans; if the value of the row is True, generate a sliding
                window from that row.
            window_stride_length (int):
                Optional. Step length used to generate input examples. Every
                ``window_stride_length`` rows will be used to generate a sliding
                window.
            window_max_count (int):
                Optional. Number of rows that should be used to generate input
                examples. If the total row count is larger than this number, the
                input data will be randomly sampled to hit the count.
            holiday_regions (List[str]):
                Optional. The geographical regions to use when creating holiday
                features. This option is only allowed when data_granularity_unit
                is ``day``. Acceptable values can come from any of the following
                levels:
                  Top level: GLOBAL
                  Second level: continental regions
                    NA: North America
                    JAPAC: Japan and Asia Pacific
                    EMEA: Europe, the Middle East and Africa
                    LAC: Latin America and the Caribbean
                  Third level: countries from ISO 3166-1 Country codes.
            sync (bool):
                Whether to execute this method synchronously. If False, this method
                will be executed in concurrent Future and any downstream object will
                be immediately returned and synced when the Future has completed.
            enable_probabilistic_inference (bool):
                If probabilistic inference is enabled, the model will fit a
                distribution that captures the uncertainty of a prediction. At
                inference time, the predictive distribution is used to make a
                point prediction that minimizes the optimization objective. For
                example, the mean of a predictive distribution is the point
                prediction that minimizes RMSE loss. If quantiles are specified,
                then the quantiles of the distribution are also returned. The
                optimization objective cannot be minimize-quantile-loss.
        Returns:
            model: The trained Vertex AI Model resource or None if training did not
                produce a Vertex AI Model.

        Raises:
            RuntimeError: If Training job has already been run or is waiting to run.
        """

        if model_display_name:
            utils.validate_display_name(model_display_name)
        if model_labels:
            utils.validate_labels(model_labels)

        if self._is_waiting_to_run():
            raise RuntimeError(
                f"{self._model_type} Forecasting Training is already scheduled "
                "to run."
            )

        if self._has_run:
            raise RuntimeError(
                f"{self._model_type} Forecasting Training has already run."
            )

        if additional_experiments:
            self._add_additional_experiments(additional_experiments)

        return self._run(
            dataset=dataset,
            target_column=target_column,
            time_column=time_column,
            time_series_identifier_column=time_series_identifier_column,
            unavailable_at_forecast_columns=unavailable_at_forecast_columns,
            available_at_forecast_columns=available_at_forecast_columns,
            forecast_horizon=forecast_horizon,
            data_granularity_unit=data_granularity_unit,
            data_granularity_count=data_granularity_count,
            training_fraction_split=training_fraction_split,
            validation_fraction_split=validation_fraction_split,
            test_fraction_split=test_fraction_split,
            predefined_split_column_name=predefined_split_column_name,
            timestamp_split_column_name=timestamp_split_column_name,
            weight_column=weight_column,
            time_series_attribute_columns=time_series_attribute_columns,
            context_window=context_window,
            budget_milli_node_hours=budget_milli_node_hours,
            export_evaluated_data_items=export_evaluated_data_items,
            export_evaluated_data_items_bigquery_destination_uri=export_evaluated_data_items_bigquery_destination_uri,
            export_evaluated_data_items_override_destination=export_evaluated_data_items_override_destination,
            quantiles=quantiles,
            validation_options=validation_options,
            model_display_name=model_display_name,
            model_labels=model_labels,
            model_id=model_id,
            parent_model=parent_model,
            is_default_version=is_default_version,
            model_version_aliases=model_version_aliases,
            model_version_description=model_version_description,
            hierarchy_group_columns=hierarchy_group_columns,
            hierarchy_group_total_weight=hierarchy_group_total_weight,
            hierarchy_temporal_total_weight=hierarchy_temporal_total_weight,
            hierarchy_group_temporal_total_weight=hierarchy_group_temporal_total_weight,
            window_column=window_column,
            window_stride_length=window_stride_length,
            window_max_count=window_max_count,
            holiday_regions=holiday_regions,
            sync=sync,
            create_request_timeout=create_request_timeout,
            enable_probabilistic_inference=enable_probabilistic_inference,
        )

    @base.optional_sync()
    def _run(
        self,
        dataset: datasets.TimeSeriesDataset,
        target_column: str,
        time_column: str,
        time_series_identifier_column: str,
        unavailable_at_forecast_columns: List[str],
        available_at_forecast_columns: List[str],
        forecast_horizon: int,
        data_granularity_unit: str,
        data_granularity_count: int,
        training_fraction_split: Optional[float] = None,
        validation_fraction_split: Optional[float] = None,
        test_fraction_split: Optional[float] = None,
        predefined_split_column_name: Optional[str] = None,
        timestamp_split_column_name: Optional[str] = None,
        weight_column: Optional[str] = None,
        time_series_attribute_columns: Optional[List[str]] = None,
        context_window: Optional[int] = None,
        export_evaluated_data_items: bool = False,
        export_evaluated_data_items_bigquery_destination_uri: Optional[str] = None,
        export_evaluated_data_items_override_destination: bool = False,
        quantiles: Optional[List[float]] = None,
        validation_options: Optional[str] = None,
        budget_milli_node_hours: int = 1000,
        model_display_name: Optional[str] = None,
        model_labels: Optional[Dict[str, str]] = None,
        model_id: Optional[str] = None,
        parent_model: Optional[str] = None,
        is_default_version: Optional[bool] = True,
        model_version_aliases: Optional[Sequence[str]] = None,
        model_version_description: Optional[str] = None,
        hierarchy_group_columns: Optional[List[str]] = None,
        hierarchy_group_total_weight: Optional[float] = None,
        hierarchy_temporal_total_weight: Optional[float] = None,
        hierarchy_group_temporal_total_weight: Optional[float] = None,
        window_column: Optional[str] = None,
        window_stride_length: Optional[int] = None,
        window_max_count: Optional[int] = None,
        holiday_regions: Optional[List[str]] = None,
        sync: bool = True,
        create_request_timeout: Optional[float] = None,
        enable_probabilistic_inference: bool = False,
    ) -> models.Model:
        """Runs the training job and returns a model.

        If training on a Vertex AI dataset, you can use one of the following split configurations:
            Data fraction splits:
            Any of ``training_fraction_split``, ``validation_fraction_split`` and
            ``test_fraction_split`` may optionally be provided, they must sum to up to 1. If
            the provided ones sum to less than 1, the remainder is assigned to sets as
            decided by Vertex AI. If none of the fractions are set, by default roughly 80%
            of data will be used for training, 10% for validation, and 10% for test.

            Predefined splits:
            Assigns input data to training, validation, and test sets based on the value of a provided key.
            If using predefined splits, ``predefined_split_column_name`` must be provided.
            Supported only for tabular Datasets.

            Timestamp splits:
            Assigns input data to training, validation, and test sets
            based on a provided timestamps. The youngest data pieces are
            assigned to training set, next to validation set, and the oldest
            to the test set.
            Supported only for tabular Datasets.

        Args:
            dataset (datasets.TimeSeriesDataset):
                Required. The dataset within the same Project from which data will be used to train the Model. The
                Dataset must use schema compatible with Model being trained,
                and what is compatible should be described in the used
                TrainingPipeline's [training_task_definition]
                [google.cloud.aiplatform.v1beta1.TrainingPipeline.training_task_definition].
                For time series Datasets, all their data is exported to
                training, to pick and choose from.
            target_column (str):
                Required. Name of the column that the Model is to predict values for. This
                column must be unavailable at forecast.
            time_column (str):
                Required. Name of the column that identifies time order in the time series.
                This column must be available at forecast.
            time_series_identifier_column (str):
                Required. Name of the column that identifies the time series.
            unavailable_at_forecast_columns (List[str]):
                Required. Column names of columns that are unavailable at forecast.
                Each column contains information for the given entity (identified by the
                [time_series_identifier_column]) that is unknown before the forecast
                (e.g. population of a city in a given year, or weather on a given day).
            available_at_forecast_columns (List[str]):
                Required. Column names of columns that are available at forecast.
                Each column contains information for the given entity (identified by the
                [time_series_identifier_column]) that is known at forecast.
            forecast_horizon: (int):
                Required. The amount of time into the future for which forecasted values for the target are
                returned. Expressed in number of units defined by the [data_granularity_unit] and
                [data_granularity_count] field. Inclusive.
            data_granularity_unit (str):
                Required. The data granularity unit. Accepted values are ``minute``,
                ``hour``, ``day``, ``week``, ``month``, ``year``.
            data_granularity_count (int):
                Required. The number of data granularity units between data points in the training
                data. If [data_granularity_unit] is `minute`, can be 1, 5, 10, 15, or 30. For all other
                values of [data_granularity_unit], must be 1.
            training_fraction_split (float):
                Optional. The fraction of the input data that is to be used to train
                the Model. This is ignored if Dataset is not provided.
            validation_fraction_split (float):
                Optional. The fraction of the input data that is to be used to validate
                the Model. This is ignored if Dataset is not provided.
            test_fraction_split (float):
                Optional. The fraction of the input data that is to be used to evaluate
                the Model. This is ignored if Dataset is not provided.
            predefined_split_column_name (str):
                Optional. The key is a name of one of the Dataset's data
                columns. The value of the key (either the label's value or
                value in the column) must be one of {``training``,
                ``validation``, ``test``}, and it defines to which set the
                given piece of data is assigned. If for a piece of data the
                key is not present or has an invalid value, that piece is
                ignored by the pipeline.

                Supported only for tabular and time series Datasets.
            timestamp_split_column_name (str):
                Optional. The key is a name of one of the Dataset's data
                columns. The value of the key values of the key (the values in
                the column) must be in RFC 3339 `date-time` format, where
                `time-offset` = `"Z"` (e.g. 1985-04-12T23:20:50.52Z). If for a
                piece of data the key is not present or has an invalid value,
                that piece is ignored by the pipeline.
                Supported only for tabular and time series Datasets.
                This parameter must be used with training_fraction_split,
                validation_fraction_split, and test_fraction_split.
            weight_column (str):
                Optional. Name of the column that should be used as the weight column.
                Higher values in this column give more importance to the row
                during Model training. The column must have numeric values between 0 and
                10000 inclusively, and 0 value means that the row is ignored.
                If the weight column field is not set, then all rows are assumed to have
                equal weight of 1.
            time_series_attribute_columns (List[str]):
                Optional. Column names that should be used as attribute columns.
                Each column is constant within a time series.
            context_window (int):
                Optional. The amount of time into the past training and prediction data is used for
                model training and prediction respectively. Expressed in number of units defined by the
                [data_granularity_unit] and [data_granularity_count] fields. When not provided uses the
                default value of 0 which means the model sets each series context window to be 0 (also
                known as "cold start"). Inclusive.
            export_evaluated_data_items (bool):
                Whether to export the test set predictions to a BigQuery table.
                If False, then the export is not performed.
            export_evaluated_data_items_bigquery_destination_uri (string):
                Optional. URI of desired destination BigQuery table for exported test set predictions.

                Expected format:
                ``bq://<project_id>:<dataset_id>:<table>``

                If not specified, then results are exported to the following auto-created BigQuery
                table:
                ``<project_id>:export_evaluated_examples_<model_name>_<yyyy_MM_dd'T'HH_mm_ss_SSS'Z'>.evaluated_examples``

                Applies only if [export_evaluated_data_items] is True.
            export_evaluated_data_items_override_destination (bool):
                Whether to override the contents of [export_evaluated_data_items_bigquery_destination_uri],
                if the table exists, for exported test set predictions. If False, and the
                table exists, then the training job will fail.

                Applies only if [export_evaluated_data_items] is True and
                [export_evaluated_data_items_bigquery_destination_uri] is specified.
            quantiles (List[float]):
                Quantiles to use for the `minimize-quantile-loss`
                [AutoMLForecastingTrainingJob.optimization_objective]. This
                argument is required in this case. Quantiles may also optionally
                be used if probabilistic inference is enabled.

                Accepts up to 5 quantiles in the form of a double from 0 to 1,
                exclusive. Each quantile must be unique.
            validation_options (str):
                Validation options for the data validation component. The available options are:
                "fail-pipeline" - (default), will validate against the validation and fail the pipeline
                                  if it fails.
                "ignore-validation" - ignore the results of the validation and continue the pipeline
            budget_milli_node_hours (int):
                Optional. The train budget of creating this Model, expressed in milli node
                hours i.e. 1,000 value in this field means 1 node hour.
                The training cost of the model will not exceed this budget. The final
                cost will be attempted to be close to the budget, though may end up
                being (even) noticeably smaller - at the backend's discretion. This
                especially may happen when further model training ceases to provide
                any improvements.
                If the budget is set to a value known to be insufficient to train a
                Model for the given training set, the training won't be attempted and
                will error.
                The minimum value is 1000 and the maximum is 72000.
            model_display_name (str):
                Optional. If the script produces a managed Vertex AI Model. The display name of
                the Model. The name can be up to 128 characters long and can be consist
                of any UTF-8 characters.

                If not provided upon creation, the job's display_name is used.
            model_labels (Dict[str, str]):
                Optional. The labels with user-defined metadata to
                organize your Models.
                Label keys and values can be no longer than 64
                characters (Unicode codepoints), can only
                contain lowercase letters, numeric characters,
                underscores and dashes. International characters
                are allowed.
                See https://goo.gl/xmQnxf for more information
                and examples of labels.
            model_id (str):
                Optional. The ID to use for the Model produced by this job,
                which will become the final component of the model resource name.
                This value may be up to 63 characters, and valid characters
                are `[a-z0-9_-]`. The first character cannot be a number or hyphen.
            parent_model (str):
                Optional. The resource name or model ID of an existing model.
                The new model uploaded by this job will be a version of `parent_model`.

                Only set this field when training a new version of an existing model.
            is_default_version (bool):
                Optional. When set to True, the newly uploaded model version will
                automatically have alias "default" included. Subsequent uses of
                the model produced by this job without a version specified will
                use this "default" version.

                When set to False, the "default" alias will not be moved.
                Actions targeting the model version produced by this job will need
                to specifically reference this version by ID or alias.

                New model uploads, i.e. version 1, will always be "default" aliased.
            model_version_aliases (Sequence[str]):
                Optional. User provided version aliases so that the model version
                uploaded by this job can be referenced via alias instead of
                auto-generated version ID. A default version alias will be created
                for the first version of the model.

                The format is [a-z][a-zA-Z0-9-]{0,126}[a-z0-9]
            model_version_description (str):
               Optional. The description of the model version being uploaded by this job.
            hierarchy_group_columns (List[str]):
                Optional. A list of time series attribute column names that
                define the time series hierarchy. Only one level of hierarchy is
                supported, ex. ``region`` for a hierarchy of stores or
                ``department`` for a hierarchy of products. If multiple columns
                are specified, time series will be grouped by their combined
                values, ex. (``blue``, ``large``) for ``color`` and ``size``, up
                to 5 columns are accepted. If no group columns are specified,
                all time series are considered to be part of the same group.
            hierarchy_group_total_weight (float):
                Optional. The weight of the loss for predictions aggregated over
                time series in the same hierarchy group.
            hierarchy_temporal_total_weight (float):
                Optional. The weight of the loss for predictions aggregated over
                the horizon for a single time series.
            hierarchy_group_temporal_total_weight (float):
                Optional. The weight of the loss for predictions aggregated over
                both the horizon and time series in the same hierarchy group.
            window_column (str):
                Optional. Name of the column that should be used to filter input
                rows. The column should contain either booleans or string
                booleans; if the value of the row is True, generate a sliding
                window from that row.
            window_stride_length (int):
                Optional. Step length used to generate input examples. Every
                ``window_stride_length`` rows will be used to generate a sliding
                window.
            window_max_count (int):
                Optional. Number of rows that should be used to generate input
                examples. If the total row count is larger than this number, the
                input data will be randomly sampled to hit the count.
            holiday_regions (List[str]):
                Optional. The geographical regions to use when creating holiday
                features. This option is only allowed when data_granularity_unit
                is ``day``. Acceptable values can come from any of the following
                levels:
                  Top level: GLOBAL
                  Second level: continental regions
                    NA: North America
                    JAPAC: Japan and Asia Pacific
                    EMEA: Europe, the Middle East and Africa
                    LAC: Latin America and the Caribbean
                  Third level: countries from ISO 3166-1 Country codes.
            sync (bool):
                Whether to execute this method synchronously. If False, this method
                will be executed in concurrent Future and any downstream object will
                be immediately returned and synced when the Future has completed.
            create_request_timeout (float):
                Optional. The timeout for the create request in seconds.
            enable_probabilistic_inference (bool):
                If probabilistic inference is enabled, the model will fit a
                distribution that captures the uncertainty of a prediction. At
                inference time, the predictive distribution is used to make a
                point prediction that minimizes the optimization objective. For
                example, the mean of a predictive distribution is the point
                prediction that minimizes RMSE loss. If quantiles are specified,
                then the quantiles of the distribution are also returned. The
                optimization objective cannot be minimize-quantile-loss.
        Returns:
            model: The trained Vertex AI Model resource or None if training did not
                produce a Vertex AI Model.
        """
        # auto-populate transformations
        if self._column_transformations is None:
            _LOGGER.info(
                "No column transformations provided, so now retrieving columns from dataset in order to set default column transformations."
            )

            (
                self._column_transformations,
                column_names,
            ) = column_transformations_utils.get_default_column_transformations(
                dataset=dataset, target_column=target_column
            )

            _LOGGER.info(
                "The column transformation of type 'auto' was set for the following columns: %s."
                % column_names
            )

        window_config = self._create_window_config(
            column=window_column,
            stride_length=window_stride_length,
            max_count=window_max_count,
        )

        # Probabilistic inference flag should be removed from additional
        # experiments in all cases since it is only an additional experiment in
        # the SDK. If both are set, always prefer job arg for setting the field.
        # TODO(b/244643824): Deprecate probabilistic inference in additional
        # experiment and only use job arg.
        additional_experiment_probabilistic_inference = (
            self._convert_enable_probabilistic_inference()
        )
        if not enable_probabilistic_inference:
            enable_probabilistic_inference = (
                additional_experiment_probabilistic_inference
            )

        training_task_inputs_dict = {
            # required inputs
            "targetColumn": target_column,
            "timeColumn": time_column,
            "timeSeriesIdentifierColumn": time_series_identifier_column,
            "timeSeriesAttributeColumns": time_series_attribute_columns,
            "unavailableAtForecastColumns": unavailable_at_forecast_columns,
            "availableAtForecastColumns": available_at_forecast_columns,
            "forecastHorizon": forecast_horizon,
            "dataGranularity": {
                "unit": data_granularity_unit,
                "quantity": data_granularity_count,
            },
            "transformations": self._column_transformations,
            "trainBudgetMilliNodeHours": budget_milli_node_hours,
            # optional inputs
            "weightColumn": weight_column,
            "contextWindow": context_window,
            "quantiles": quantiles,
            "validationOptions": validation_options,
            "optimizationObjective": self._optimization_objective,
            "holidayRegions": holiday_regions,
        }

        # TODO(TheMichaelHu): Remove the ifs once the API supports these inputs.
        if any(
            [
                hierarchy_group_columns,
                hierarchy_group_total_weight,
                hierarchy_temporal_total_weight,
                hierarchy_group_temporal_total_weight,
            ]
        ):
            training_task_inputs_dict["hierarchyConfig"] = {
                "groupColumns": hierarchy_group_columns,
                "groupTotalWeight": hierarchy_group_total_weight,
                "temporalTotalWeight": hierarchy_temporal_total_weight,
                "groupTemporalTotalWeight": hierarchy_group_temporal_total_weight,
            }
        if window_config:
            training_task_inputs_dict["windowConfig"] = window_config

        if enable_probabilistic_inference:
            training_task_inputs_dict[
                "enableProbabilisticInference"
            ] = enable_probabilistic_inference

        final_export_eval_bq_uri = export_evaluated_data_items_bigquery_destination_uri
        if final_export_eval_bq_uri and not final_export_eval_bq_uri.startswith(
            "bq://"
        ):
            final_export_eval_bq_uri = f"bq://{final_export_eval_bq_uri}"

        if export_evaluated_data_items:
            training_task_inputs_dict["exportEvaluatedDataItemsConfig"] = {
                "destinationBigqueryUri": final_export_eval_bq_uri,
                "overrideExistingTable": export_evaluated_data_items_override_destination,
            }

        if self._additional_experiments:
            training_task_inputs_dict[
                "additionalExperiments"
            ] = self._additional_experiments

        model = gca_model.Model(
            display_name=model_display_name or self._display_name,
            labels=model_labels or self._labels,
            encryption_spec=self._model_encryption_spec,
        )

        new_model = self._run_job(
            training_task_definition=self._training_task_definition,
            training_task_inputs=training_task_inputs_dict,
            dataset=dataset,
            training_fraction_split=training_fraction_split,
            validation_fraction_split=validation_fraction_split,
            test_fraction_split=test_fraction_split,
            predefined_split_column_name=predefined_split_column_name,
            timestamp_split_column_name=timestamp_split_column_name,
            model=model,
            model_id=model_id,
            parent_model=parent_model,
            is_default_version=is_default_version,
            model_version_aliases=model_version_aliases,
            model_version_description=model_version_description,
            create_request_timeout=create_request_timeout,
        )

        if export_evaluated_data_items:
            _LOGGER.info(
                "Exported examples available at:\n%s"
                % self.evaluated_data_items_bigquery_uri
            )

        return new_model

    @property
    def _model_upload_fail_string(self) -> str:
        """Helper property for model upload failure."""
        return (
            f"Training Pipeline {self.resource_name} is not configured to upload a "
            "Model."
        )

    @property
    def evaluated_data_items_bigquery_uri(self) -> Optional[str]:
        """BigQuery location of exported evaluated examples from the Training Job
        Returns:
            str: BigQuery uri for the exported evaluated examples if the export
                feature is enabled for training.
            None: If the export feature was not enabled for training.
        """

        self._assert_gca_resource_is_available()

        metadata = self._gca_resource.training_task_metadata
        if metadata and "evaluatedDataItemsBigqueryUri" in metadata:
            return metadata["evaluatedDataItemsBigqueryUri"]

        return None

    def _add_additional_experiments(self, additional_experiments: List[str]):
        """Add experiment flags to the training job.
        Args:
            additional_experiments (List[str]):
                Experiment flags that can enable some experimental training features.
        """
        self._additional_experiments.extend(additional_experiments)

    def _convert_enable_probabilistic_inference(self) -> bool:
        """Convert enable probabilistic from additional experiments."""
        key = "enable_probabilistic_inference"
        if self._additional_experiments:
            if key in self._additional_experiments:
                self._additional_experiments.remove(key)
                return True
        return False

    @staticmethod
    def _create_window_config(
        column: Optional[str] = None,
        stride_length: Optional[int] = None,
        max_count: Optional[int] = None,
    ) -> Optional[Dict[str, Union[int, str]]]:
        """Creates a window config from training job arguments."""
        configs = {
            "column": column,
            "strideLength": stride_length,
            "maxCount": max_count,
        }
        present_configs = {k: v for k, v in configs.items() if v is not None}
        if not present_configs:
            return None
        if len(present_configs) > 1:
            raise ValueError(
                "More than one windowing strategy provided. Make sure only one "
                "of window_column, window_stride_length, or window_max_count "
                "is specified."
            )
        return present_configs


# TODO(b/172368325) add scheduling, custom_job.Scheduling
class CustomTrainingJob(_CustomTrainingJob):
    """Class to launch a Custom Training Job in Vertex AI using a script.

    Takes a training implementation as a python script and executes that
    script in Cloud Vertex AI Training.
    """

    def __init__(
        self,
        # TODO(b/223262536): Make display_name parameter fully optional in next major release
        display_name: str,
        script_path: str,
        container_uri: str,
        requirements: Optional[Sequence[str]] = None,
        model_serving_container_image_uri: Optional[str] = None,
        model_serving_container_predict_route: Optional[str] = None,
        model_serving_container_health_route: Optional[str] = None,
        model_serving_container_command: Optional[Sequence[str]] = None,
        model_serving_container_args: Optional[Sequence[str]] = None,
        model_serving_container_environment_variables: Optional[Dict[str, str]] = None,
        model_serving_container_ports: Optional[Sequence[int]] = None,
        model_description: Optional[str] = None,
        model_instance_schema_uri: Optional[str] = None,
        model_parameters_schema_uri: Optional[str] = None,
        model_prediction_schema_uri: Optional[str] = None,
        explanation_metadata: Optional[explain.ExplanationMetadata] = None,
        explanation_parameters: Optional[explain.ExplanationParameters] = None,
        project: Optional[str] = None,
        location: Optional[str] = None,
        credentials: Optional[auth_credentials.Credentials] = None,
        labels: Optional[Dict[str, str]] = None,
        training_encryption_spec_key_name: Optional[str] = None,
        model_encryption_spec_key_name: Optional[str] = None,
        staging_bucket: Optional[str] = None,
    ):
        """Constructs a Custom Training Job from a Python script.

        job = aiplatform.CustomTrainingJob(
            display_name='test-train',
            script_path='test_script.py',
            requirements=['pandas', 'numpy'],
            container_uri='gcr.io/cloud-aiplatform/training/tf-cpu.2-2:latest',
            model_serving_container_image_uri='gcr.io/my-trainer/serving:1',
            model_serving_container_predict_route='predict',
            model_serving_container_health_route='metadata,
            labels={'key': 'value'},
        )

        Usage with Dataset:

        ds = aiplatform.TabularDataset(
            'projects/my-project/locations/us-central1/datasets/12345')

        job.run(
            ds,
            replica_count=1,
            model_display_name='my-trained-model',
            model_labels={'key': 'value'},
        )

        Usage without Dataset:

        job.run(replica_count=1, model_display_name='my-trained-model)


        To ensure your model gets saved in Vertex AI, write your saved model to
        os.environ["AIP_MODEL_DIR"] in your provided training script.


        Args:
            display_name (str):
                Required. The user-defined name of this TrainingPipeline.
            script_path (str): Required. Local path to training script.
            container_uri (str):
                Required: Uri of the training container image in the GCR.
            requirements (Sequence[str]):
                List of python packages dependencies of script.
            model_serving_container_image_uri (str):
                If the training produces a managed Vertex AI Model, the URI of the
                Model serving container suitable for serving the model produced by the
                training script.
            model_serving_container_predict_route (str):
                If the training produces a managed Vertex AI Model, An HTTP path to
                send prediction requests to the container, and which must be supported
                by it. If not specified a default HTTP path will be used by Vertex AI.
            model_serving_container_health_route (str):
                If the training produces a managed Vertex AI Model, an HTTP path to
                send health check requests to the container, and which must be supported
                by it. If not specified a standard HTTP path will be used by AI
                Platform.
            model_serving_container_command (Sequence[str]):
                The command with which the container is run. Not executed within a
                shell. The Docker image's ENTRYPOINT is used if this is not provided.
                Variable references $(VAR_NAME) are expanded using the container's
                environment. If a variable cannot be resolved, the reference in the
                input string will be unchanged. The $(VAR_NAME) syntax can be escaped
                with a double $$, ie: $$(VAR_NAME). Escaped references will never be
                expanded, regardless of whether the variable exists or not.
            model_serving_container_args (Sequence[str]):
                The arguments to the command. The Docker image's CMD is used if this is
                not provided. Variable references $(VAR_NAME) are expanded using the
                container's environment. If a variable cannot be resolved, the reference
                in the input string will be unchanged. The $(VAR_NAME) syntax can be
                escaped with a double $$, ie: $$(VAR_NAME). Escaped references will
                never be expanded, regardless of whether the variable exists or not.
            model_serving_container_environment_variables (Dict[str, str]):
                The environment variables that are to be present in the container.
                Should be a dictionary where keys are environment variable names
                and values are environment variable values for those names.
            model_serving_container_ports (Sequence[int]):
                Declaration of ports that are exposed by the container. This field is
                primarily informational, it gives Vertex AI information about the
                network connections the container uses. Listing or not a port here has
                no impact on whether the port is actually exposed, any port listening on
                the default "0.0.0.0" address inside a container will be accessible from
                the network.
            model_description (str):
                The description of the Model.
            model_instance_schema_uri (str):
                Optional. Points to a YAML file stored on Google Cloud
                Storage describing the format of a single instance, which
                are used in
                ``PredictRequest.instances``,
                ``ExplainRequest.instances``
                and
                ``BatchPredictionJob.input_config``.
                The schema is defined as an OpenAPI 3.0.2 `Schema
                Object <https://tinyurl.com/y538mdwt#schema-object>`__.
                AutoML Models always have this field populated by AI
                Platform. Note: The URI given on output will be immutable
                and probably different, including the URI scheme, than the
                one given on input. The output URI will point to a location
                where the user only has a read access.
            model_parameters_schema_uri (str):
                Optional. Points to a YAML file stored on Google Cloud
                Storage describing the parameters of prediction and
                explanation via
                ``PredictRequest.parameters``,
                ``ExplainRequest.parameters``
                and
                ``BatchPredictionJob.model_parameters``.
                The schema is defined as an OpenAPI 3.0.2 `Schema
                Object <https://tinyurl.com/y538mdwt#schema-object>`__.
                AutoML Models always have this field populated by AI
                Platform, if no parameters are supported it is set to an
                empty string. Note: The URI given on output will be
                immutable and probably different, including the URI scheme,
                than the one given on input. The output URI will point to a
                location where the user only has a read access.
            model_prediction_schema_uri (str):
                Optional. Points to a YAML file stored on Google Cloud
                Storage describing the format of a single prediction
                produced by this Model, which are returned via
                ``PredictResponse.predictions``,
                ``ExplainResponse.explanations``,
                and
                ``BatchPredictionJob.output_config``.
                The schema is defined as an OpenAPI 3.0.2 `Schema
                Object <https://tinyurl.com/y538mdwt#schema-object>`__.
                AutoML Models always have this field populated by AI
                Platform. Note: The URI given on output will be immutable
                and probably different, including the URI scheme, than the
                one given on input. The output URI will point to a location
                where the user only has a read access.
            explanation_metadata (explain.ExplanationMetadata):
                Optional. Metadata describing the Model's input and output for
                explanation. `explanation_metadata` is optional while
                `explanation_parameters` must be specified when used.
                For more details, see `Ref docs <http://tinyurl.com/1igh60kt>`
            explanation_parameters (explain.ExplanationParameters):
                Optional. Parameters to configure explaining for Model's
                predictions.
                For more details, see `Ref docs <http://tinyurl.com/1an4zake>`
            project (str):
                Project to run training in. Overrides project set in aiplatform.init.
            location (str):
                Location to run training in. Overrides location set in aiplatform.init.
            credentials (auth_credentials.Credentials):
                Custom credentials to use to run call training service. Overrides
                credentials set in aiplatform.init.
            labels (Dict[str, str]):
                Optional. The labels with user-defined metadata to
                organize TrainingPipelines.
                Label keys and values can be no longer than 64
                characters (Unicode codepoints), can only
                contain lowercase letters, numeric characters,
                underscores and dashes. International characters
                are allowed.
                See https://goo.gl/xmQnxf for more information
                and examples of labels.
            training_encryption_spec_key_name (Optional[str]):
                Optional. The Cloud KMS resource identifier of the customer
                managed encryption key used to protect the training pipeline. Has the
                form:
                ``projects/my-project/locations/my-region/keyRings/my-kr/cryptoKeys/my-key``.
                The key needs to be in the same region as where the compute
                resource is created.

                If set, this TrainingPipeline will be secured by this key.

                Note: Model trained by this TrainingPipeline is also secured
                by this key if ``model_to_upload`` is not set separately.

                Overrides encryption_spec_key_name set in aiplatform.init.
            model_encryption_spec_key_name (Optional[str]):
                Optional. The Cloud KMS resource identifier of the customer
                managed encryption key used to protect the model. Has the
                form:
                ``projects/my-project/locations/my-region/keyRings/my-kr/cryptoKeys/my-key``.
                The key needs to be in the same region as where the compute
                resource is created.

                If set, the trained Model will be secured by this key.

                Overrides encryption_spec_key_name set in aiplatform.init.
            staging_bucket (str):
                Bucket used to stage source and training artifacts. Overrides
                staging_bucket set in aiplatform.init.
        """
        if not display_name:
            display_name = self.__class__._generate_display_name()
        super().__init__(
            display_name=display_name,
            project=project,
            location=location,
            credentials=credentials,
            labels=labels,
            training_encryption_spec_key_name=training_encryption_spec_key_name,
            model_encryption_spec_key_name=model_encryption_spec_key_name,
            container_uri=container_uri,
            model_instance_schema_uri=model_instance_schema_uri,
            model_parameters_schema_uri=model_parameters_schema_uri,
            model_prediction_schema_uri=model_prediction_schema_uri,
            model_serving_container_environment_variables=model_serving_container_environment_variables,
            model_serving_container_ports=model_serving_container_ports,
            model_serving_container_image_uri=model_serving_container_image_uri,
            model_serving_container_command=model_serving_container_command,
            model_serving_container_args=model_serving_container_args,
            model_serving_container_predict_route=model_serving_container_predict_route,
            model_serving_container_health_route=model_serving_container_health_route,
            model_description=model_description,
            explanation_metadata=explanation_metadata,
            explanation_parameters=explanation_parameters,
            staging_bucket=staging_bucket,
        )

        self._requirements = requirements
        self._script_path = script_path

    def run(
        self,
        dataset: Optional[
            Union[
                datasets.ImageDataset,
                datasets.TabularDataset,
                datasets.TextDataset,
                datasets.VideoDataset,
            ]
        ] = None,
        annotation_schema_uri: Optional[str] = None,
        model_display_name: Optional[str] = None,
        model_labels: Optional[Dict[str, str]] = None,
        model_id: Optional[str] = None,
        parent_model: Optional[str] = None,
        is_default_version: Optional[bool] = True,
        model_version_aliases: Optional[Sequence[str]] = None,
        model_version_description: Optional[str] = None,
        base_output_dir: Optional[str] = None,
        service_account: Optional[str] = None,
        network: Optional[str] = None,
        bigquery_destination: Optional[str] = None,
        args: Optional[List[Union[str, float, int]]] = None,
        environment_variables: Optional[Dict[str, str]] = None,
        replica_count: int = 1,
        machine_type: str = "n1-standard-4",
        accelerator_type: str = "ACCELERATOR_TYPE_UNSPECIFIED",
        accelerator_count: int = 0,
        boot_disk_type: str = "pd-ssd",
        boot_disk_size_gb: int = 100,
        reduction_server_replica_count: int = 0,
        reduction_server_machine_type: Optional[str] = None,
        reduction_server_container_uri: Optional[str] = None,
        training_fraction_split: Optional[float] = None,
        validation_fraction_split: Optional[float] = None,
        test_fraction_split: Optional[float] = None,
        training_filter_split: Optional[str] = None,
        validation_filter_split: Optional[str] = None,
        test_filter_split: Optional[str] = None,
        predefined_split_column_name: Optional[str] = None,
        timestamp_split_column_name: Optional[str] = None,
        timeout: Optional[int] = None,
        restart_job_on_worker_restart: bool = False,
        enable_web_access: bool = False,
        enable_dashboard_access: bool = False,
        tensorboard: Optional[str] = None,
        sync=True,
        create_request_timeout: Optional[float] = None,
        disable_retries: bool = False,
        persistent_resource_id: Optional[str] = None,
        tpu_topology: Optional[str] = None,
    ) -> Optional[models.Model]:
        """Runs the custom training job.

        You can configure a custom training job as a distributed training job by
        specifying multiple *replicas*. To define more than one replica, set
        `replica_count` to a number greater than one. For example, if you
        specify 10 for `replica_count`, then one chief replica is provisioned
        and nine replicas are created that make up a *worker pool*. All replicas
        in the worker pool have the same `machine_type`, `accelerator_type`, and
        `accelerator_count`. For more information, see
        [Distributed training](https://cloud.google.com/vertex-ai/docs/training/distributed-training).

        If you train on a Vertex AI dataset, you can use one of the following
        [split configurations](https://cloud.google.com/vertex-ai/docs/tabular-data/data-splits#default_data_split):

        * [Random
        split](https://cloud.google.com/vertex-ai/docs/tabular-data/data-splits#classification-random):
        The random split is also known as *mathematical split* or *fraction
        split*. By default, the percentages of training data used for the
        training, validation, and test sets are 80, 10, and 10, respectively. If
        you are using Google Cloud console, you can change the percentages to
        any values that add up to 100. If you are using the Vertex AI SDK, you
        use fractions that add up to 1.0. Use the optional
        `training_fraction_split`, `validation_fraction_split`, and
        `test_fraction_split` to change the percentages. If the specified
        fractions total less than 1.0, then Vertex AI specifies the remainder.

        * [Data filter
        split](https://cloud.google.com/vertex-ai/docs/general/ml-use#filter):
        Assigns input data to training, validation, and test sets. For example,
        can set `training_filter_split` to `labels.flower=rose`,
        `validation_filter_split` to `labels.flower=daisy`,`test_filter_split`
        to `labels.flower=dahlia`. With these settings, data labeled as rose is
        added to the training set, data labeled as daisy is added to the
        validation set, and data labeled as dahlia is added to the test set. If
        you use filter splits, you need to specify all three filters. If
        you don't want a filter to match any items, set it to the minus
        sign (`-`). Data filter splits are supported only for unstructured
        datasets that contain dataitems.

        * [Manual
        split](https://cloud.google.com/vertex-ai/docs/tabular-data/data-splits#classification-manual):
        The manual split is also known as a *predefined split*. A manual split
        assigns input data to training, validation, and test sets based on the
        value of a provided key. You must specify the
        `predefined_split_column_name` to use a manual split. Manual splits are
        supported only for tabular datasets.

        * [Chronological
        split](https://cloud.google.com/vertex-ai/docs/tabular-data/data-splits#classification-time):
        The chronological split is also known as a *timestamp split*. If your
        data is time-dependent, you can use the `timestamp_split_column_name`
        parameter to designate one column as a time column. Vertex AI uses the
        time column to split your data, with the earliest of the rows used for
        training, the next rows for validation, and the latest rows for testing.
        Chronological splits are supported only for tabular Datasets.

        Args:
            dataset (
                Union[
                    datasets.ImageDataset,
                    datasets.TabularDataset,
                    datasets.TextDataset,
                    datasets.VideoDataset,
                ]
            ):
                Optional. When you create a
                [custom training pipeline](https://cloud.google.com/vertex-ai/docs/training/create-training-pipeline),
                you can specify that your training application uses a Vertex AI
                dataset. At runtime, Vertex AI passes metadata about your
                dataset to your training application by setting the following
                environment variables in your training container.
                `AIP_DATA_FORMAT` is the format that your dataset is exported
                in. Possible values include: `jsonl`, `csv`, or `bigquery`.
                `AIP_TRAINING_DATA_URI` is the BigQuery URI of your training
                data or the Cloud Storage URI of your training data file.
                `AIP_VALIDATION_DATA_URI` is the BigQuery URI for your
                validation data or the Cloud Storage URI of your validation data
                file. `AIP_TEST_DATA_URI` is the BigQuery URI for your test data
                or the Cloud Storage URI of your test data file. An example of
                how you set a dataset is `aip_training_data_uri =
                os.environ.get('AIP_TRAINING_DATA_URI')`. For more information,
                see [Access a dataset from your training
                application](https://cloud.google.com/vertex-ai/docs/training/using-managed-datasets#access_a_dataset_from_your_training_application).

            annotation_schema_uri (str):
                Optional. A Cloud Storage URI that points to a YAML file
                describing an annotation schema. The schema is defined as an
                OpenAPI 3.0.2 [Schema
                Object](https://github.com/OAI/OpenAPI-Specification/blob/main/versions/3.0.2.md#schema-object).
                The schema files that can be used are in the following Cloud
                Storage bucket:
                `gs://google-cloud-aiplatform/schema/dataset/annotation/`. The
                schema you choose must be consistent with the `metadata` of the
                Dataset specified by `dataset_id`.

                If you use a split method, then only annotations that match this
                schema and belong to `DataItems` that are not ignored by the
                split method are used. The `DataItems` are used for training,
                validation, or testing, depending on their role.

                If you use an annotations filter, then the annotations used for
                training are filtered using the annotations filter and the
                `annotation_schema_uri`.
            model_display_name (str):
                Optional. The user-defined name of the model. The name must
                contain 128 or fewer UTF-8 characters. If not specified, then
                the 'display_name` of the training job is used.
            model_labels (Dict[str, str]):
                Optional. Labels with user-defined metadata to organize your
                models. The maximum length of a key and of a
                value is 64 unicode characters. Labels and keys can contain only
                lowercase letters, numeric characters, underscores, and dashes.
                International characters are allowed. No more than 64 user
                labels can be associated with one Tensorboard (system labels are
                excluded). For more information and examples of using labels, see
                [Using labels to organize Google Cloud Platform resources](https://goo.gl/xmQnxf).
                System reserved label keys are prefixed with
                `aiplatform.googleapis.com/` and are immutable.
            model_id (str):
                Optional. The ID to use for the model produced by the training
                job. The. `model_id` is the final component of the model
                resource name. The maximum length of the `model_id` is63
                characters, and valid characters are `[a-z0-9_-]`. The first
                character cannot be a number or hyphen.
            parent_model (str):
                Optional. The resource name or model ID of an existing model.
                The new model uploaded by the training job is a version of
                `parent_model`. Set `parent_model` only when training a new
                version of an existing model.
            is_default_version (bool):
                Optional. When set to `true`, the newly uploaded model version
                includes the alias `default`. Subsequent models produced by a
                training job that don't have a version specified use the default
                version. When set to `false`, the `default` alias isn't assigned
                to the model and verion one of the model is always the default.
                Actions targeting the model version produced by this job need to
                reference this version by its ID or alias.
            model_version_aliases (Sequence[str]):
                Optional. User provided version aliases that can be used instead
                of an auto-generated ID to reference the model version uploaded
                by the training job. A default version alias is created for the
                first version of the model. The format is
                `[a-z][a-zA-Z0-9-]{0,126}[a-z0-9]`.
            model_version_description (str):
               Optional. The description of the model version that's uploaded by
               this training job.
            base_output_dir (str):
                Optional. The Cloud Storage output directory of the training
                job. If not provided, a timestamped directory in the staging
                directory is used.

                Vertex AI sets the following environment variables when it runs
                your training code:

                `AIP_MODEL_DIR` - a Cloud Storage URI of a directory used to
                save model artifacts, such as `<base_output_dir>/model/`.
                `AIP_CHECKPOINT_DIR`: a Cloud Storage URI of a directory used to
                save checkpoints, such as `<base_output_dir>/checkpoints/`.
                `AIP_TENSORBOARD_LOG_DIR`: a Cloud Storage URI of a directory
                used to save TensorBoard logs, such as
                `<base_output_dir>/logs/`.

            service_account (str):
                Optional. A service account used to run the pipeline training
                job. To submit a pipeline training job using a service account,
                a user needs to have the `iam.serviceAccounts.actAs` permission
                on the service account. For more information, see [Requiring
                permission to attach service accounts to
                resources](https://cloud.google.com/iam/docs/service-accounts-actas).
            network (str):
                Optional. The full name of the Compute Engine network to which the job
                should be peered. For example, projects/12345/global/networks/myVPC.
                Private services access must already be configured for the network.
                If left unspecified, the network set by `aiplatform.init` is used
                and the pipeline job is not peered with any network.
            bigquery_destination (str):
                Optional. When using a BigQuery dataset, this is the BigQuery
                project location to where the training data is written. A new
                dataset is created in the specified project with the name
                `dataset_<dataset-id>_<annotation-type>_<timestamp-of-training-call>`,
                where the timestamp is in the `YYYY_MM_DDThh_mm_ss_sssZ` format.
                All training input data is written into the new dataset that
                includes three tables: `training`, `validation`, and `test`.

                -  AIP_DATA_FORMAT = "bigquery".
                -  AIP_TRAINING_DATA_URI ="bigquery_destination.dataset_*.training"
                -  AIP_VALIDATION_DATA_URI = "bigquery_destination.dataset_*.validation"
                -  AIP_TEST_DATA_URI = "bigquery_destination.dataset_*.test"
            args (List[Unions[str, int, float]]):
                Optional. Command line arguments that are passed to the Python
                script.
            environment_variables (Dict[str, str]):
                Optional. Environment variables that are passed to the container.
                The need to be a dictionary where keys are environment variable names
                and values are the environment variable values for those names.
                The maximum number of environment variables you can specify is
                10 and each environment variable name must be unique. The following
                shows the format of an environment variable:

                ```py
                environment_variables = {
                    'MY_KEY': 'MY_VALUE'
                }
                ```
            replica_count (int):
                The number of worker replicas. If one replica is specified, then
                one chief replica is provisioned. To define more than one
                replica so you can have a worker pool, set `replica_count` to a
                number greater than one. For example, if you specify 10 for
                `replica_count`, then one chief replica is provisioned and nine
                replicas are created that make up a *worker pool*. All replicas
                in the worker pool have the same `machine_type`,
                `accelerator_type`, and `accelerator_count`. For more
                information, see [Distributed
                training](https://cloud.google.com/vertex-ai/docs/training/distributed-training).
            machine_type (str):
                The type of machine to use for training.
            accelerator_type (str):
                The hardware accelerator type. You can specify one of the
                following: `ACCELERATOR_TYPE_UNSPECIFIED`, `NVIDIA_TESLA_K80`,
                `NVIDIA_TESLA_P100`, `NVIDIA_TESLA_V100`, `NVIDIA_TESLA_P4`,
                `NVIDIA_TESLA_T4`.
            accelerator_count (int):
                The number of accelerators to attach to a worker replica.
            boot_disk_type (str):
                Type of the boot disk. The valid values are `pd-ssd` (Persistent
                Disk Solid State Drive) and `pd-standard` (Persistent Disk Hard
                Disk Drive). The default value is `pd-ssd`.
            boot_disk_size_gb (int):
                The boot disk size in GB. The default is 100GB. The minimum
                size is 100 and the maximum size is 64,000.
            reduction_server_replica_count (int):
                The number of reduction server replicas. The default value is
                `0`.
            reduction_server_machine_type (str):
                Optional. The type of machine to use for a reduction server.
            reduction_server_container_uri (str):
                Optional. The URI of the reduction server container image.
                For more information, see
                [Reduce training time with Reduction Server](https://cloud.google.com/vertex-ai/docs/training/distributed-training#reduce_training_time_with_reduction_server).
            training_fraction_split (float):
                Optional. The fraction of the input data used to train
                the model if a dataset is provided. If a dataset isn't provided,
                then this is ignored.
            validation_fraction_split (float):
                Optional. The fraction of the input data used to validate
                the model if a dataset is provided. If a dataset isn't provided,
                then this is ignored.
            test_fraction_split (float):
                Optional. The fraction of the input data used to evaluate the
                model if a dataset is provided. If a dataset isn't provided,
                then this is ignored.
            training_filter_split (str):
                Optional. A training split filter on the data items in a
                dataset. Data items that match the filter are used to train the
                model. You can use a filter with the same syntax as the one used
                in `DatasetService.ListDataItems`. This filter is used to train
                a model. If a single data item is matched by more than one of
                the training split filters, then it's assigned to the training
                set. If a dataset isn't provided, then it's ignored. For more
                information, see [Data splits for tabular
                data](https://cloud.google.com/vertex-ai/docs/tabular-data/data-splits).
            validation_filter_split (str):
                Optional. A validation split filter on the data items in a
                dataset. You can use a filter with the same syntax as the one
                used in `DatasetService.ListDataItems`. This filter is used to
                validate the model. You can use a filter with the same syntax as
                the one used in `DatasetService.ListDataItems`. If a single data
                item is matched by more than one of the validation split
                filters, then it's assigned to the validation set. If a dataset
                isn't provided, it's ignored. For more information, see
                [Data splits for tabular
                data](https://cloud.google.com/vertex-ai/docs/tabular-data/data-splits).
            test_filter_split (str):
                Optional. A test split filter on the data items in a dataset.
                You can use a filter with the same syntax as the one used in
                `DatasetService.ListDataItems`. This filter is used to test the
                model. You can use a filter with the same syntax as the one used
                in `DatasetService.ListDataItems`. If a single data item is
                matched by more than one of the test split filters, then
                it's assigned to the test set. If a dataset isn't
                provided, it's ignored. For more information, see [Data
                splits for tabular
                data](https://cloud.google.com/vertex-ai/docs/tabular-data/data-splits).
            predefined_split_column_name (str):
                Optional. A key-value pair where the key is a name of one of the
                data columns in the dataset. The value of the key (either the
                label's value or value in the column) must be `training`,
                `validation`, or `test`. The value specifies the set to which
                the data is assigned. Data that doesn't have a key, or that has
                an invalid value, is ignored. The `predefined_split_column_name`
                is supported by only tabular and time series Datasets.
            timestamp_split_column_name (str):
                Optional. A key-value pair where the key is a name of one of the
                data columns in the dataset. The value of each key is the values
                in the column. Each value must be in the
                [RFC 3339]](https://www.rfc-editor.org/rfc/rfc3339) `date-time`
                format, where `time-offset` = `"Z"` (for example,
                1985-04-12T23:20:50.52Z). Data that doesn't have a key, or that
                has an invalid value, is ignored. The
                `timestamp_split_column_name` is supported by only tabular and
                time series Datasets.
            timeout (int):
                Optional.The maximum duration that a pipeline training job can
                run. `timeout` is specified in seconds. The default is 80,400
                seconds (7 days).
            restart_job_on_worker_restart (bool):
                If set to `true`, the custom job of a worker is restarted. You
                might set this to `true` if your distributed training job isn't
                resilient to workers leaving and joining a job. The default
                value is `false`.
            enable_web_access (bool):
                If set to `true`, Vertex AI enables interactive shell access
                to training containers. For more information, see
                [Monitor and debug training with an interactive shell](https://cloud.google.com/vertex-ai/docs/training/monitor-debug-interactive-shell).
                The default value is `false`.
            enable_dashboard_access (bool):
                If set to `true`, Vertex AI enables access to the customized
                dashboard for training containers. The default value is `false`.
            tensorboard (str):
                Optional. The name of a Vertex AI
                [Tensorboard](https://cloud.google.com/python/docs/reference/aiplatform/latest/google.cloud.aiplatform.Tensorboard)
                resource to which this customer pipeline training job uploads
                tensorboard logs. Use the following format to specify the
                Tensorboard:
                `projects/{project}/locations/{location}/tensorboards/{tensorboard}`.
                The training script writes Tensorboard to the
                `AIP_TENSORBOARD_LOG_DIR` Vertex AI environment variable. If you
                use a Tensorboard, then you need to specify the
                `service_account` parameter. For more information, see
                [Use Vertex AI TensorBoard with custom training](https://cloud.google.com/vertex-ai/docs/experiments/tensorboard-training).
            sync (bool):
                If set to `true`, this runs synchronously. If `false`, this
                method runs asynchronously.
            create_request_timeout (float):
                Optional. The timeout for the create request in seconds.
            disable_retries (bool):
                If set to `true`, the job retries for internal errors after the
                job starts running. If set to `true`,
                `restart_job_on_worker_restart` is overridden and set to
                `false`.
            persistent_resource_id (str):
                Optional. The ID of the PersistentResource in the same Project
                and Location. If this is specified, the job will be run on
                existing machines held by the PersistentResource instead of
                on-demand short-live machines. The network, CMEK, and node pool
                configs on the job should be consistent with those on the
                PersistentResource, otherwise, the job will be rejected.
            tpu_topology (str):
                Optional. Specifies the tpu topology to be used for
                TPU training job. This field is required for TPU v5 versions. For
                details on the TPU topology, refer to
                https://cloud.google.com/tpu/docs/v5e#tpu-v5e-config. The topology must
                be a supported value for the TPU machine type.

        Returns:
            The trained Vertex AI model resource or None if the training
            job didn't create a model.
        """
        network = network or initializer.global_config.network
        service_account = service_account or initializer.global_config.service_account

        worker_pool_specs, managed_model = self._prepare_and_validate_run(
            model_display_name=model_display_name,
            model_labels=model_labels,
            replica_count=replica_count,
            machine_type=machine_type,
            accelerator_count=accelerator_count,
            accelerator_type=accelerator_type,
            boot_disk_type=boot_disk_type,
            boot_disk_size_gb=boot_disk_size_gb,
            reduction_server_replica_count=reduction_server_replica_count,
            reduction_server_machine_type=reduction_server_machine_type,
            tpu_topology=tpu_topology,
        )

        # make and copy package
        python_packager = source_utils._TrainingScriptPythonPackager(
            script_path=self._script_path, requirements=self._requirements
        )

        return self._run(
            python_packager=python_packager,
            dataset=dataset,
            annotation_schema_uri=annotation_schema_uri,
            worker_pool_specs=worker_pool_specs,
            managed_model=managed_model,
            model_id=model_id,
            parent_model=parent_model,
            is_default_version=is_default_version,
            model_version_aliases=model_version_aliases,
            model_version_description=model_version_description,
            args=args,
            environment_variables=environment_variables,
            base_output_dir=base_output_dir,
            service_account=service_account,
            network=network,
            bigquery_destination=bigquery_destination,
            training_fraction_split=training_fraction_split,
            validation_fraction_split=validation_fraction_split,
            test_fraction_split=test_fraction_split,
            training_filter_split=training_filter_split,
            validation_filter_split=validation_filter_split,
            test_filter_split=test_filter_split,
            predefined_split_column_name=predefined_split_column_name,
            timestamp_split_column_name=timestamp_split_column_name,
            timeout=timeout,
            restart_job_on_worker_restart=restart_job_on_worker_restart,
            enable_web_access=enable_web_access,
            enable_dashboard_access=enable_dashboard_access,
            tensorboard=tensorboard,
            reduction_server_container_uri=reduction_server_container_uri
            if reduction_server_replica_count > 0
            else None,
            sync=sync,
            create_request_timeout=create_request_timeout,
            disable_retries=disable_retries,
            persistent_resource_id=persistent_resource_id,
        )

    def submit(
        self,
        dataset: Optional[
            Union[
                datasets.ImageDataset,
                datasets.TabularDataset,
                datasets.TextDataset,
                datasets.VideoDataset,
            ]
        ] = None,
        annotation_schema_uri: Optional[str] = None,
        model_display_name: Optional[str] = None,
        model_labels: Optional[Dict[str, str]] = None,
        model_id: Optional[str] = None,
        parent_model: Optional[str] = None,
        is_default_version: Optional[bool] = True,
        model_version_aliases: Optional[Sequence[str]] = None,
        model_version_description: Optional[str] = None,
        base_output_dir: Optional[str] = None,
        service_account: Optional[str] = None,
        network: Optional[str] = None,
        bigquery_destination: Optional[str] = None,
        args: Optional[List[Union[str, float, int]]] = None,
        environment_variables: Optional[Dict[str, str]] = None,
        replica_count: int = 1,
        machine_type: str = "n1-standard-4",
        accelerator_type: str = "ACCELERATOR_TYPE_UNSPECIFIED",
        accelerator_count: int = 0,
        boot_disk_type: str = "pd-ssd",
        boot_disk_size_gb: int = 100,
        reduction_server_replica_count: int = 0,
        reduction_server_machine_type: Optional[str] = None,
        reduction_server_container_uri: Optional[str] = None,
        training_fraction_split: Optional[float] = None,
        validation_fraction_split: Optional[float] = None,
        test_fraction_split: Optional[float] = None,
        training_filter_split: Optional[str] = None,
        validation_filter_split: Optional[str] = None,
        test_filter_split: Optional[str] = None,
        predefined_split_column_name: Optional[str] = None,
        timestamp_split_column_name: Optional[str] = None,
        timeout: Optional[int] = None,
        restart_job_on_worker_restart: bool = False,
        enable_web_access: bool = False,
        enable_dashboard_access: bool = False,
        tensorboard: Optional[str] = None,
        sync=True,
        create_request_timeout: Optional[float] = None,
        disable_retries: bool = False,
        persistent_resource_id: Optional[str] = None,
        tpu_topology: Optional[str] = None,
    ) -> Optional[models.Model]:
        """Submits the custom training job without blocking until completion.

        Distributed Training Support:
        If replica count = 1 then one chief replica will be provisioned. If
        replica_count > 1 the remainder will be provisioned as a worker replica pool.
        ie: replica_count = 10 will result in 1 chief and 9 workers
        All replicas have same machine_type, accelerator_type, and accelerator_count

        If training on a Vertex AI dataset, you can use one of the following split configurations:
            Data fraction splits:
            Any of ``training_fraction_split``, ``validation_fraction_split`` and
            ``test_fraction_split`` may optionally be provided, they must sum to up to 1. If
            the provided ones sum to less than 1, the remainder is assigned to sets as
            decided by Vertex AI. If none of the fractions are set, by default roughly 80%
            of data will be used for training, 10% for validation, and 10% for test.

            Data filter splits:
            Assigns input data to training, validation, and test sets
            based on the given filters, data pieces not matched by any
            filter are ignored. Currently only supported for Datasets
            containing DataItems.
            If any of the filters in this message are to match nothing, then
            they can be set as '-' (the minus sign).
            If using filter splits, all of ``training_filter_split``, ``validation_filter_split`` and
            ``test_filter_split`` must be provided.
            Supported only for unstructured Datasets.

            Predefined splits:
            Assigns input data to training, validation, and test sets based on the value of a provided key.
            If using predefined splits, ``predefined_split_column_name`` must be provided.
            Supported only for tabular Datasets.

            Timestamp splits:
            Assigns input data to training, validation, and test sets
            based on a provided timestamps. The youngest data pieces are
            assigned to training set, next to validation set, and the oldest
            to the test set.
            Supported only for tabular Datasets.

        Args:
            dataset (
                Union[
                    datasets.ImageDataset,
                    datasets.TabularDataset,
                    datasets.TextDataset,
                    datasets.VideoDataset,
                ]
            ):
                Vertex AI to fit this training against. Custom training script should
                retrieve datasets through passed in environment variables uris:

                os.environ["AIP_TRAINING_DATA_URI"]
                os.environ["AIP_VALIDATION_DATA_URI"]
                os.environ["AIP_TEST_DATA_URI"]

                Additionally the dataset format is passed in as:

                os.environ["AIP_DATA_FORMAT"]
            annotation_schema_uri (str):
                Google Cloud Storage URI points to a YAML file describing
                annotation schema. The schema is defined as an OpenAPI 3.0.2
                [Schema Object](https://github.com/OAI/OpenAPI-Specification/blob/main/versions/3.0.2.md#schema-object) The schema files
                that can be used here are found in
                gs://google-cloud-aiplatform/schema/dataset/annotation/,
                note that the chosen schema must be consistent with
                ``metadata``
                of the Dataset specified by
                ``dataset_id``.

                Only Annotations that both match this schema and belong to
                DataItems not ignored by the split method are used in
                respectively training, validation or test role, depending on
                the role of the DataItem they are on.

                When used in conjunction with
                ``annotations_filter``,
                the Annotations used for training are filtered by both
                ``annotations_filter``
                and
                ``annotation_schema_uri``.
            model_display_name (str):
                If the script produces a managed Vertex AI Model. The display name of
                the Model. The name can be up to 128 characters long and can be consist
                of any UTF-8 characters.

                If not provided upon creation, the job's display_name is used.
            model_labels (Dict[str, str]):
                Optional. The labels with user-defined metadata to
                organize your Models.
                Label keys and values can be no longer than 64
                characters (Unicode codepoints), can only
                contain lowercase letters, numeric characters,
                underscores and dashes. International characters
                are allowed.
                See https://goo.gl/xmQnxf for more information
                and examples of labels.
            model_id (str):
                Optional. The ID to use for the Model produced by this job,
                which will become the final component of the model resource name.
                This value may be up to 63 characters, and valid characters
                are `[a-z0-9_-]`. The first character cannot be a number or hyphen.
            parent_model (str):
                Optional. The resource name or model ID of an existing model.
                The new model uploaded by this job will be a version of `parent_model`.

                Only set this field when training a new version of an existing model.
            is_default_version (bool):
                Optional. When set to True, the newly uploaded model version will
                automatically have alias "default" included. Subsequent uses of
                the model produced by this job without a version specified will
                use this "default" version.

                When set to False, the "default" alias will not be moved.
                Actions targeting the model version produced by this job will need
                to specifically reference this version by ID or alias.

                New model uploads, i.e. version 1, will always be "default" aliased.
            model_version_aliases (Sequence[str]):
                Optional. User provided version aliases so that the model version
                uploaded by this job can be referenced via alias instead of
                auto-generated version ID. A default version alias will be created
                for the first version of the model.

                The format is [a-z][a-zA-Z0-9-]{0,126}[a-z0-9]
            model_version_description (str):
               Optional. The description of the model version being uploaded by this job.
            base_output_dir (str):
                GCS output directory of job. If not provided a
                timestamped directory in the staging directory will be used.

                Vertex AI sets the following environment variables when it runs your training code:

                -  AIP_MODEL_DIR: a Cloud Storage URI of a directory intended for saving model artifacts, i.e. <base_output_dir>/model/
                -  AIP_CHECKPOINT_DIR: a Cloud Storage URI of a directory intended for saving checkpoints, i.e. <base_output_dir>/checkpoints/
                -  AIP_TENSORBOARD_LOG_DIR: a Cloud Storage URI of a directory intended for saving TensorBoard logs, i.e. <base_output_dir>/logs/

            service_account (str):
                Specifies the service account for workload run-as account.
                Users submitting jobs must have act-as permission on this run-as account.
            network (str):
                The full name of the Compute Engine network to which the job
                should be peered. For example, projects/12345/global/networks/myVPC.
                Private services access must already be configured for the network.
                If left unspecified, the network set in aiplatform.init will be used.
                Otherwise, the job is not peered with any network.
            bigquery_destination (str):
                Provide this field if `dataset` is a BigQuery dataset.
                The BigQuery project location where the training data is to
                be written to. In the given project a new dataset is created
                with name
                ``dataset_<dataset-id>_<annotation-type>_<timestamp-of-training-call>``
                where timestamp is in YYYY_MM_DDThh_mm_ss_sssZ format. All
                training input data will be written into that dataset. In
                the dataset three tables will be created, ``training``,
                ``validation`` and ``test``.

                -  AIP_DATA_FORMAT = "bigquery".
                -  AIP_TRAINING_DATA_URI ="bigquery_destination.dataset_*.training"
                -  AIP_VALIDATION_DATA_URI = "bigquery_destination.dataset_*.validation"
                -  AIP_TEST_DATA_URI = "bigquery_destination.dataset_*.test"
            args (List[Unions[str, int, float]]):
                Command line arguments to be passed to the Python script.
            environment_variables (Dict[str, str]):
                Environment variables to be passed to the container.
                Should be a dictionary where keys are environment variable names
                and values are environment variable values for those names.
                At most 10 environment variables can be specified.
                The Name of the environment variable must be unique.

                environment_variables = {
                    'MY_KEY': 'MY_VALUE'
                }
            replica_count (int):
                The number of worker replicas. If replica count = 1 then one chief
                replica will be provisioned. If replica_count > 1 the remainder will be
                provisioned as a worker replica pool.
            machine_type (str):
                The type of machine to use for training.
            accelerator_type (str):
                Hardware accelerator type. One of ACCELERATOR_TYPE_UNSPECIFIED,
                NVIDIA_TESLA_K80, NVIDIA_TESLA_P100, NVIDIA_TESLA_V100, NVIDIA_TESLA_P4,
                NVIDIA_TESLA_T4
            accelerator_count (int):
                The number of accelerators to attach to a worker replica.
            boot_disk_type (str):
                Type of the boot disk, default is `pd-ssd`.
                Valid values: `pd-ssd` (Persistent Disk Solid State Drive) or
                `pd-standard` (Persistent Disk Hard Disk Drive).
            boot_disk_size_gb (int):
                Size in GB of the boot disk, default is 100GB.
                boot disk size must be within the range of [100, 64000].
            reduction_server_replica_count (int):
                The number of reduction server replicas, default is 0.
            reduction_server_machine_type (str):
                Optional. The type of machine to use for reduction server.
            reduction_server_container_uri (str):
                Optional. The Uri of the reduction server container image.
                See details: https://cloud.google.com/vertex-ai/docs/training/distributed-training#reduce_training_time_with_reduction_server
            training_fraction_split (float):
                Optional. The fraction of the input data that is to be used to train
                the Model. This is ignored if Dataset is not provided.
            validation_fraction_split (float):
                Optional. The fraction of the input data that is to be used to validate
                the Model. This is ignored if Dataset is not provided.
            test_fraction_split (float):
                Optional. The fraction of the input data that is to be used to evaluate
                the Model. This is ignored if Dataset is not provided.
            training_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to train the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            validation_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to validate the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            test_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to test the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            predefined_split_column_name (str):
                Optional. The key is a name of one of the Dataset's data
                columns. The value of the key (either the label's value or
                value in the column) must be one of {``training``,
                ``validation``, ``test``}, and it defines to which set the
                given piece of data is assigned. If for a piece of data the
                key is not present or has an invalid value, that piece is
                ignored by the pipeline.

                Supported only for tabular and time series Datasets.
            timestamp_split_column_name (str):
                Optional. The key is a name of one of the Dataset's data
                columns. The value of the key values of the key (the values in
                the column) must be in RFC 3339 `date-time` format, where
                `time-offset` = `"Z"` (e.g. 1985-04-12T23:20:50.52Z). If for a
                piece of data the key is not present or has an invalid value,
                that piece is ignored by the pipeline.

                Supported only for tabular and time series Datasets.
            timeout (int):
                The maximum job running time in seconds. The default is 7 days.
            restart_job_on_worker_restart (bool):
                Restarts the entire CustomJob if a worker
                gets restarted. This feature can be used by
                distributed training jobs that are not resilient
                to workers leaving and joining a job.
            enable_web_access (bool):
                Whether you want Vertex AI to enable interactive shell access
                to training containers.
                https://cloud.google.com/vertex-ai/docs/training/monitor-debug-interactive-shell
            enable_dashboard_access (bool):
                Whether you want Vertex AI to enable access to the customized dashboard
                to training containers.
            tensorboard (str):
                Optional. The name of a Vertex AI
                [Tensorboard][google.cloud.aiplatform.v1beta1.Tensorboard]
                resource to which this CustomJob will upload Tensorboard
                logs. Format:
                ``projects/{project}/locations/{location}/tensorboards/{tensorboard}``

                The training script should write Tensorboard to following Vertex AI environment
                variable:

                AIP_TENSORBOARD_LOG_DIR

                `service_account` is required with provided `tensorboard`.
                For more information on configuring your service account please visit:
                https://cloud.google.com/vertex-ai/docs/experiments/tensorboard-training
            create_request_timeout (float):
                Optional. The timeout for the create request in seconds.
            sync (bool):
                Whether to execute this method synchronously. If False, this method
                will be executed in concurrent Future and any downstream object will
                be immediately returned and synced when the Future has completed.
            disable_retries (bool):
                Indicates if the job should retry for internal errors after the
                job starts running. If True, overrides
                `restart_job_on_worker_restart` to False.
            persistent_resource_id (str):
                Optional. The ID of the PersistentResource in the same Project
                and Location. If this is specified, the job will be run on
                existing machines held by the PersistentResource instead of
                on-demand short-live machines. The network, CMEK, and node pool
                configs on the job should be consistent with those on the
                PersistentResource, otherwise, the job will be rejected.
            tpu_topology (str):
                Optional. Specifies the tpu topology to be used for
                TPU training job. This field is required for TPU v5 versions. For
                details on the TPU topology, refer to
                https://cloud.google.com/tpu/docs/v5e#tpu-v5e-config. The topology must
                be a supported value for the TPU machine type.

        Returns:
            model: The trained Vertex AI Model resource or None if training did not
                produce a Vertex AI Model.
        """
        network = network or initializer.global_config.network

        worker_pool_specs, managed_model = self._prepare_and_validate_run(
            model_display_name=model_display_name,
            model_labels=model_labels,
            replica_count=replica_count,
            machine_type=machine_type,
            accelerator_count=accelerator_count,
            accelerator_type=accelerator_type,
            boot_disk_type=boot_disk_type,
            boot_disk_size_gb=boot_disk_size_gb,
            reduction_server_replica_count=reduction_server_replica_count,
            reduction_server_machine_type=reduction_server_machine_type,
            tpu_topology=tpu_topology,
        )

        # make and copy package
        python_packager = source_utils._TrainingScriptPythonPackager(
            script_path=self._script_path, requirements=self._requirements
        )

        return self._run(
            python_packager=python_packager,
            dataset=dataset,
            annotation_schema_uri=annotation_schema_uri,
            worker_pool_specs=worker_pool_specs,
            managed_model=managed_model,
            model_id=model_id,
            parent_model=parent_model,
            is_default_version=is_default_version,
            model_version_aliases=model_version_aliases,
            model_version_description=model_version_description,
            args=args,
            environment_variables=environment_variables,
            base_output_dir=base_output_dir,
            service_account=service_account,
            network=network,
            bigquery_destination=bigquery_destination,
            training_fraction_split=training_fraction_split,
            validation_fraction_split=validation_fraction_split,
            test_fraction_split=test_fraction_split,
            training_filter_split=training_filter_split,
            validation_filter_split=validation_filter_split,
            test_filter_split=test_filter_split,
            predefined_split_column_name=predefined_split_column_name,
            timestamp_split_column_name=timestamp_split_column_name,
            timeout=timeout,
            restart_job_on_worker_restart=restart_job_on_worker_restart,
            enable_web_access=enable_web_access,
            enable_dashboard_access=enable_dashboard_access,
            tensorboard=tensorboard,
            reduction_server_container_uri=reduction_server_container_uri
            if reduction_server_replica_count > 0
            else None,
            sync=sync,
            create_request_timeout=create_request_timeout,
            block=False,
            disable_retries=disable_retries,
            persistent_resource_id=persistent_resource_id,
        )

    @base.optional_sync(construct_object_on_arg="managed_model")
    def _run(
        self,
        python_packager: source_utils._TrainingScriptPythonPackager,
        dataset: Optional[
            Union[
                datasets.ImageDataset,
                datasets.TabularDataset,
                datasets.TextDataset,
                datasets.VideoDataset,
            ]
        ],
        annotation_schema_uri: Optional[str],
        worker_pool_specs: worker_spec_utils._DistributedTrainingSpec,
        managed_model: Optional[gca_model.Model] = None,
        model_id: Optional[str] = None,
        parent_model: Optional[str] = None,
        is_default_version: Optional[bool] = True,
        model_version_aliases: Optional[Sequence[str]] = None,
        model_version_description: Optional[str] = None,
        args: Optional[List[Union[str, float, int]]] = None,
        environment_variables: Optional[Dict[str, str]] = None,
        base_output_dir: Optional[str] = None,
        service_account: Optional[str] = None,
        network: Optional[str] = None,
        bigquery_destination: Optional[str] = None,
        training_fraction_split: Optional[float] = None,
        validation_fraction_split: Optional[float] = None,
        test_fraction_split: Optional[float] = None,
        training_filter_split: Optional[str] = None,
        validation_filter_split: Optional[str] = None,
        test_filter_split: Optional[str] = None,
        predefined_split_column_name: Optional[str] = None,
        timestamp_split_column_name: Optional[str] = None,
        timeout: Optional[int] = None,
        restart_job_on_worker_restart: bool = False,
        enable_web_access: bool = False,
        enable_dashboard_access: bool = False,
        tensorboard: Optional[str] = None,
        reduction_server_container_uri: Optional[str] = None,
        sync=True,
        create_request_timeout: Optional[float] = None,
        block: Optional[bool] = True,
        disable_retries: bool = False,
        persistent_resource_id: Optional[str] = None,
    ) -> Optional[models.Model]:
        """Packages local script and launches training_job.

        Args:
            python_packager (source_utils._TrainingScriptPythonPackager):
                Required. Python Packager pointing to training script locally.
            dataset (
                Union[
                    datasets.ImageDataset,
                    datasets.TabularDataset,
                    datasets.TextDataset,
                    datasets.VideoDataset,
                ]
            ):
                Vertex AI to fit this training against.
            annotation_schema_uri (str):
                Google Cloud Storage URI points to a YAML file describing
                annotation schema.
            worker_pools_spec (worker_spec_utils._DistributedTrainingSpec):
                Worker pools pecs required to run job.
            managed_model (gca_model.Model):
                Model proto if this script produces a Managed Model.
            model_id (str):
                Optional. The ID to use for the Model produced by this job,
                which will become the final component of the model resource name.
                This value may be up to 63 characters, and valid characters
                are `[a-z0-9_-]`. The first character cannot be a number or hyphen.
            parent_model (str):
                Optional. The resource name or model ID of an existing model.
                The new model uploaded by this job will be a version of `parent_model`.

                Only set this field when training a new version of an existing model.
            is_default_version (bool):
                Optional. When set to True, the newly uploaded model version will
                automatically have alias "default" included. Subsequent uses of
                the model produced by this job without a version specified will
                use this "default" version.

                When set to False, the "default" alias will not be moved.
                Actions targeting the model version produced by this job will need
                to specifically reference this version by ID or alias.

                New model uploads, i.e. version 1, will always be "default" aliased.
            model_version_aliases (Sequence[str]):
                Optional. User provided version aliases so that the model version
                uploaded by this job can be referenced via alias instead of
                auto-generated version ID. A default version alias will be created
                for the first version of the model.

                The format is [a-z][a-zA-Z0-9-]{0,126}[a-z0-9]
            model_version_description (str):
               Optional. The description of the model version being uploaded by this job.
            args (List[Unions[str, int, float]]):
                Command line arguments to be passed to the Python script.
            environment_variables (Dict[str, str]):
                Environment variables to be passed to the container.
                Should be a dictionary where keys are environment variable names
                and values are environment variable values for those names.
                At most 10 environment variables can be specified.
                The Name of the environment variable must be unique.

                environment_variables = {
                    'MY_KEY': 'MY_VALUE'
                }
            base_output_dir (str):
                GCS output directory of job. If not provided a
                timestamped directory in the staging directory will be used.

                Vertex AI sets the following environment variables when it runs your training code:

                -  AIP_MODEL_DIR: a Cloud Storage URI of a directory intended for saving model artifacts, i.e. <base_output_dir>/model/
                -  AIP_CHECKPOINT_DIR: a Cloud Storage URI of a directory intended for saving checkpoints, i.e. <base_output_dir>/checkpoints/
                -  AIP_TENSORBOARD_LOG_DIR: a Cloud Storage URI of a directory intended for saving TensorBoard logs, i.e. <base_output_dir>/logs/

            service_account (str):
                Specifies the service account for workload run-as account.
                Users submitting jobs must have act-as permission on this run-as account.
            network (str):
                The full name of the Compute Engine network to which the job
                should be peered. For example, projects/12345/global/networks/myVPC.
                Private services access must already be configured for the network.
                If left unspecified, the job is not peered with any network.
            bigquery_destination (str):
                Provide this field if `dataset` is a BigQuery dataset.
                The BigQuery project location where the training data is to
                be written to. In the given project a new dataset is created
                with name
                ``dataset_<dataset-id>_<annotation-type>_<timestamp-of-training-call>``
                where timestamp is in YYYY_MM_DDThh_mm_ss_sssZ format. All
                training input data will be written into that dataset. In
                the dataset three tables will be created, ``training``,
                ``validation`` and ``test``.

                -  AIP_DATA_FORMAT = "bigquery".
                -  AIP_TRAINING_DATA_URI ="bigquery_destination.dataset_*.training"
                -  AIP_VALIDATION_DATA_URI = "bigquery_destination.dataset_*.validation"
                -  AIP_TEST_DATA_URI = "bigquery_destination.dataset_*.test"
            training_fraction_split (float):
                Optional. The fraction of the input data that is to be used to train
                the Model. This is ignored if Dataset is not provided.
            validation_fraction_split (float):
                Optional. The fraction of the input data that is to be used to validate
                the Model. This is ignored if Dataset is not provided.
            test_fraction_split (float):
                Optional. The fraction of the input data that is to be used to evaluate
                the Model. This is ignored if Dataset is not provided.
            training_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to train the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            validation_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to validate the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            test_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to test the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            predefined_split_column_name (str):
                Optional. The key is a name of one of the Dataset's data
                columns. The value of the key (either the label's value or
                value in the column) must be one of {``training``,
                ``validation``, ``test``}, and it defines to which set the
                given piece of data is assigned. If for a piece of data the
                key is not present or has an invalid value, that piece is
                ignored by the pipeline.

                Supported only for tabular and time series Datasets.
            timestamp_split_column_name (str):
                Optional. The key is a name of one of the Dataset's data
                columns. The value of the key values of the key (the values in
                the column) must be in RFC 3339 `date-time` format, where
                `time-offset` = `"Z"` (e.g. 1985-04-12T23:20:50.52Z). If for a
                piece of data the key is not present or has an invalid value,
                that piece is ignored by the pipeline.

                Supported only for tabular and time series Datasets.
            timeout (int):
                The maximum job running time in seconds. The default is 7 days.
            restart_job_on_worker_restart (bool):
                Restarts the entire CustomJob if a worker
                gets restarted. This feature can be used by
                distributed training jobs that are not resilient
                to workers leaving and joining a job.
            enable_web_access (bool):
                Whether you want Vertex AI to enable interactive shell access
                to training containers.
                https://cloud.google.com/vertex-ai/docs/training/monitor-debug-interactive-shell
            enable_dashboard_access (bool):
                Whether you want Vertex AI to enable access to the customized dashboard
                to training containers.
            tensorboard (str):
                Optional. The name of a Vertex AI
                [Tensorboard][google.cloud.aiplatform.v1beta1.Tensorboard]
                resource to which this CustomJob will upload Tensorboard
                logs. Format:
                ``projects/{project}/locations/{location}/tensorboards/{tensorboard}``

                The training script should write Tensorboard to following Vertex AI environment
                variable:

                AIP_TENSORBOARD_LOG_DIR

                `service_account` is required with provided `tensorboard`.
                For more information on configuring your service account please visit:
                https://cloud.google.com/vertex-ai/docs/experiments/tensorboard-training
            reduction_server_container_uri (str):
                Optional. The Uri of the reduction server container image.
            sync (bool):
                Whether to execute this method synchronously. If False, this method
                will be executed in concurrent Future and any downstream object will
                be immediately returned and synced when the Future has completed.
            create_request_timeout (float)
                Optional. The timeout for the create request in seconds
            block (bool):
                Optional. If True, block until complete.
            disable_retries (bool):
                Indicates if the job should retry for internal errors after the
                job starts running. If True, overrides
                `restart_job_on_worker_restart` to False.
            persistent_resource_id (str):
                Optional. The ID of the PersistentResource in the same Project
                and Location. If this is specified, the job will be run on
                existing machines held by the PersistentResource instead of
                on-demand short-live machines. The network, CMEK, and node pool
                configs on the job should be consistent with those on the
                PersistentResource, otherwise, the job will be rejected.

        Returns:
            model: The trained Vertex AI Model resource or None if training did not
                produce a Vertex AI Model.
        """
        package_gcs_uri = python_packager.package_and_copy_to_gcs(
            gcs_staging_dir=self._staging_bucket,
            project=self.project,
            credentials=self.credentials,
        )

        for spec_order, spec in enumerate(worker_pool_specs):

            if not spec:
                continue

            if (
                spec_order == worker_spec_utils._SPEC_ORDERS["server_spec"]
                and reduction_server_container_uri
            ):
                spec["container_spec"] = {
                    "image_uri": reduction_server_container_uri,
                }
            else:
                spec["python_package_spec"] = {
                    "executor_image_uri": self._container_uri,
                    "python_module": python_packager.module_name,
                    "package_uris": [package_gcs_uri],
                }

                if args:
                    spec["python_package_spec"]["args"] = args

                if environment_variables:
                    spec["python_package_spec"]["env"] = [
                        {"name": key, "value": value}
                        for key, value in environment_variables.items()
                    ]

        (
            training_task_inputs,
            base_output_dir,
        ) = self._prepare_training_task_inputs_and_output_dir(
            worker_pool_specs=worker_pool_specs,
            base_output_dir=base_output_dir,
            service_account=service_account,
            network=network,
            timeout=timeout,
            restart_job_on_worker_restart=restart_job_on_worker_restart,
            enable_web_access=enable_web_access,
            enable_dashboard_access=enable_dashboard_access,
            tensorboard=tensorboard,
            disable_retries=disable_retries,
            persistent_resource_id=persistent_resource_id,
        )

        model = self._run_job(
            training_task_definition=schema.training_job.definition.custom_task,
            training_task_inputs=training_task_inputs,
            dataset=dataset,
            annotation_schema_uri=annotation_schema_uri,
            training_fraction_split=training_fraction_split,
            validation_fraction_split=validation_fraction_split,
            test_fraction_split=test_fraction_split,
            training_filter_split=training_filter_split,
            validation_filter_split=validation_filter_split,
            test_filter_split=test_filter_split,
            predefined_split_column_name=predefined_split_column_name,
            timestamp_split_column_name=timestamp_split_column_name,
            model=managed_model,
            model_id=model_id,
            parent_model=parent_model,
            is_default_version=is_default_version,
            model_version_aliases=model_version_aliases,
            model_version_description=model_version_description,
            gcs_destination_uri_prefix=base_output_dir,
            bigquery_destination=bigquery_destination,
            create_request_timeout=create_request_timeout,
            block=block,
        )

        return model


class CustomContainerTrainingJob(_CustomTrainingJob):
    """Class to launch a Custom Training Job in Vertex AI using a
    Container."""

    def __init__(
        self,
        # TODO(b/223262536): Make display_name parameter fully optional in next major release
        display_name: str,
        container_uri: str,
        command: Sequence[str] = None,
        model_serving_container_image_uri: Optional[str] = None,
        model_serving_container_predict_route: Optional[str] = None,
        model_serving_container_health_route: Optional[str] = None,
        model_serving_container_command: Optional[Sequence[str]] = None,
        model_serving_container_args: Optional[Sequence[str]] = None,
        model_serving_container_environment_variables: Optional[Dict[str, str]] = None,
        model_serving_container_ports: Optional[Sequence[int]] = None,
        model_description: Optional[str] = None,
        model_instance_schema_uri: Optional[str] = None,
        model_parameters_schema_uri: Optional[str] = None,
        model_prediction_schema_uri: Optional[str] = None,
        explanation_metadata: Optional[explain.ExplanationMetadata] = None,
        explanation_parameters: Optional[explain.ExplanationParameters] = None,
        project: Optional[str] = None,
        location: Optional[str] = None,
        credentials: Optional[auth_credentials.Credentials] = None,
        labels: Optional[Dict[str, str]] = None,
        training_encryption_spec_key_name: Optional[str] = None,
        model_encryption_spec_key_name: Optional[str] = None,
        staging_bucket: Optional[str] = None,
    ):
        """Constructs a Custom Container Training Job.

        job = aiplatform.CustomContainerTrainingJob(
            display_name='test-train',
            container_uri='gcr.io/my_project_id/my_image_name:tag',
            command=['python3', 'run_script.py']
            model_serving_container_image_uri='gcr.io/my-trainer/serving:1',
            model_serving_container_predict_route='predict',
            model_serving_container_health_route='metadata,
            labels={'key': 'value'},
        )

        Usage with Dataset:

        ds = aiplatform.TabularDataset(
            'projects/my-project/locations/us-central1/datasets/12345')

        job.run(
            ds,
            replica_count=1,
            model_display_name='my-trained-model',
            model_labels={'key': 'value'},
        )

        Usage without Dataset:

        job.run(replica_count=1, model_display_name='my-trained-model)


        To ensure your model gets saved in Vertex AI, write your saved model to
        os.environ["AIP_MODEL_DIR"] in your provided training script.


        Args:
            display_name (str):
                Required. The user-defined name of this TrainingPipeline.
            container_uri (str):
                Required: Uri of the training container image in the GCR.
            command (Sequence[str]):
                The command to be invoked when the container is started.
                It overrides the entrypoint instruction in Dockerfile when provided
            model_serving_container_image_uri (str):
                If the training produces a managed Vertex AI Model, the URI of the
                Model serving container suitable for serving the model produced by the
                training script.
            model_serving_container_predict_route (str):
                If the training produces a managed Vertex AI Model, An HTTP path to
                send prediction requests to the container, and which must be supported
                by it. If not specified a default HTTP path will be used by Vertex AI.
            model_serving_container_health_route (str):
                If the training produces a managed Vertex AI Model, an HTTP path to
                send health check requests to the container, and which must be supported
                by it. If not specified a standard HTTP path will be used by AI
                Platform.
            model_serving_container_command (Sequence[str]):
                The command with which the container is run. Not executed within a
                shell. The Docker image's ENTRYPOINT is used if this is not provided.
                Variable references $(VAR_NAME) are expanded using the container's
                environment. If a variable cannot be resolved, the reference in the
                input string will be unchanged. The $(VAR_NAME) syntax can be escaped
                with a double $$, ie: $$(VAR_NAME). Escaped references will never be
                expanded, regardless of whether the variable exists or not.
            model_serving_container_args (Sequence[str]):
                The arguments to the command. The Docker image's CMD is used if this is
                not provided. Variable references $(VAR_NAME) are expanded using the
                container's environment. If a variable cannot be resolved, the reference
                in the input string will be unchanged. The $(VAR_NAME) syntax can be
                escaped with a double $$, ie: $$(VAR_NAME). Escaped references will
                never be expanded, regardless of whether the variable exists or not.
            model_serving_container_environment_variables (Dict[str, str]):
                The environment variables that are to be present in the container.
                Should be a dictionary where keys are environment variable names
                and values are environment variable values for those names.
            model_serving_container_ports (Sequence[int]):
                Declaration of ports that are exposed by the container. This field is
                primarily informational, it gives Vertex AI information about the
                network connections the container uses. Listing or not a port here has
                no impact on whether the port is actually exposed, any port listening on
                the default "0.0.0.0" address inside a container will be accessible from
                the network.
            model_description (str):
                The description of the Model.
            model_instance_schema_uri (str):
                Optional. Points to a YAML file stored on Google Cloud
                Storage describing the format of a single instance, which
                are used in
                ``PredictRequest.instances``,
                ``ExplainRequest.instances``
                and
                ``BatchPredictionJob.input_config``.
                The schema is defined as an OpenAPI 3.0.2 `Schema
                Object <https://tinyurl.com/y538mdwt#schema-object>`__.
                AutoML Models always have this field populated by AI
                Platform. Note: The URI given on output will be immutable
                and probably different, including the URI scheme, than the
                one given on input. The output URI will point to a location
                where the user only has a read access.
            model_parameters_schema_uri (str):
                Optional. Points to a YAML file stored on Google Cloud
                Storage describing the parameters of prediction and
                explanation via
                ``PredictRequest.parameters``,
                ``ExplainRequest.parameters``
                and
                ``BatchPredictionJob.model_parameters``.
                The schema is defined as an OpenAPI 3.0.2 `Schema
                Object <https://tinyurl.com/y538mdwt#schema-object>`__.
                AutoML Models always have this field populated by AI
                Platform, if no parameters are supported it is set to an
                empty string. Note: The URI given on output will be
                immutable and probably different, including the URI scheme,
                than the one given on input. The output URI will point to a
                location where the user only has a read access.
            model_prediction_schema_uri (str):
                Optional. Points to a YAML file stored on Google Cloud
                Storage describing the format of a single prediction
                produced by this Model, which are returned via
                ``PredictResponse.predictions``,
                ``ExplainResponse.explanations``,
                and
                ``BatchPredictionJob.output_config``.
                The schema is defined as an OpenAPI 3.0.2 `Schema
                Object <https://tinyurl.com/y538mdwt#schema-object>`__.
                AutoML Models always have this field populated by AI
                Platform. Note: The URI given on output will be immutable
                and probably different, including the URI scheme, than the
                one given on input. The output URI will point to a location
                where the user only has a read access.
            explanation_metadata (explain.ExplanationMetadata):
                Optional. Metadata describing the Model's input and output for
                explanation. `explanation_metadata` is optional while
                `explanation_parameters` must be specified when used.
                For more details, see `Ref docs <http://tinyurl.com/1igh60kt>`
            explanation_parameters (explain.ExplanationParameters):
                Optional. Parameters to configure explaining for Model's
                predictions.
                For more details, see `Ref docs <http://tinyurl.com/1an4zake>`
            project (str):
                Project to run training in. Overrides project set in aiplatform.init.
            location (str):
                Location to run training in. Overrides location set in aiplatform.init.
            credentials (auth_credentials.Credentials):
                Custom credentials to use to run call training service. Overrides
                credentials set in aiplatform.init.
            labels (Dict[str, str]):
                Optional. The labels with user-defined metadata to
                organize TrainingPipelines.
                Label keys and values can be no longer than 64
                characters (Unicode codepoints), can only
                contain lowercase letters, numeric characters,
                underscores and dashes. International characters
                are allowed.
                See https://goo.gl/xmQnxf for more information
                and examples of labels.
            training_encryption_spec_key_name (Optional[str]):
                Optional. The Cloud KMS resource identifier of the customer
                managed encryption key used to protect the training pipeline. Has the
                form:
                ``projects/my-project/locations/my-region/keyRings/my-kr/cryptoKeys/my-key``.
                The key needs to be in the same region as where the compute
                resource is created.

                If set, this TrainingPipeline will be secured by this key.

                Note: Model trained by this TrainingPipeline is also secured
                by this key if ``model_to_upload`` is not set separately.

                Overrides encryption_spec_key_name set in aiplatform.init.
            model_encryption_spec_key_name (Optional[str]):
                Optional. The Cloud KMS resource identifier of the customer
                managed encryption key used to protect the model. Has the
                form:
                ``projects/my-project/locations/my-region/keyRings/my-kr/cryptoKeys/my-key``.
                The key needs to be in the same region as where the compute
                resource is created.

                If set, the trained Model will be secured by this key.

                Overrides encryption_spec_key_name set in aiplatform.init.
            staging_bucket (str):
                Bucket used to stage source and training artifacts. Overrides
                staging_bucket set in aiplatform.init.
        """
        if not display_name:
            display_name = self.__class__._generate_display_name()
        super().__init__(
            display_name=display_name,
            project=project,
            location=location,
            credentials=credentials,
            labels=labels,
            training_encryption_spec_key_name=training_encryption_spec_key_name,
            model_encryption_spec_key_name=model_encryption_spec_key_name,
            container_uri=container_uri,
            model_instance_schema_uri=model_instance_schema_uri,
            model_parameters_schema_uri=model_parameters_schema_uri,
            model_prediction_schema_uri=model_prediction_schema_uri,
            model_serving_container_environment_variables=model_serving_container_environment_variables,
            model_serving_container_ports=model_serving_container_ports,
            model_serving_container_image_uri=model_serving_container_image_uri,
            model_serving_container_command=model_serving_container_command,
            model_serving_container_args=model_serving_container_args,
            model_serving_container_predict_route=model_serving_container_predict_route,
            model_serving_container_health_route=model_serving_container_health_route,
            model_description=model_description,
            explanation_metadata=explanation_metadata,
            explanation_parameters=explanation_parameters,
            staging_bucket=staging_bucket,
        )

        self._command = command

    def run(
        self,
        dataset: Optional[
            Union[
                datasets.ImageDataset,
                datasets.TabularDataset,
                datasets.TextDataset,
                datasets.VideoDataset,
            ]
        ] = None,
        annotation_schema_uri: Optional[str] = None,
        model_display_name: Optional[str] = None,
        model_labels: Optional[Dict[str, str]] = None,
        model_id: Optional[str] = None,
        parent_model: Optional[str] = None,
        is_default_version: Optional[bool] = True,
        model_version_aliases: Optional[Sequence[str]] = None,
        model_version_description: Optional[str] = None,
        base_output_dir: Optional[str] = None,
        service_account: Optional[str] = None,
        network: Optional[str] = None,
        bigquery_destination: Optional[str] = None,
        args: Optional[List[Union[str, float, int]]] = None,
        environment_variables: Optional[Dict[str, str]] = None,
        replica_count: int = 1,
        machine_type: str = "n1-standard-4",
        accelerator_type: str = "ACCELERATOR_TYPE_UNSPECIFIED",
        accelerator_count: int = 0,
        boot_disk_type: str = "pd-ssd",
        boot_disk_size_gb: int = 100,
        reduction_server_replica_count: int = 0,
        reduction_server_machine_type: Optional[str] = None,
        reduction_server_container_uri: Optional[str] = None,
        training_fraction_split: Optional[float] = None,
        validation_fraction_split: Optional[float] = None,
        test_fraction_split: Optional[float] = None,
        training_filter_split: Optional[str] = None,
        validation_filter_split: Optional[str] = None,
        test_filter_split: Optional[str] = None,
        predefined_split_column_name: Optional[str] = None,
        timestamp_split_column_name: Optional[str] = None,
        timeout: Optional[int] = None,
        restart_job_on_worker_restart: bool = False,
        enable_web_access: bool = False,
        enable_dashboard_access: bool = False,
        tensorboard: Optional[str] = None,
        sync=True,
        create_request_timeout: Optional[float] = None,
        disable_retries: bool = False,
        persistent_resource_id: Optional[str] = None,
        tpu_topology: Optional[str] = None,
    ) -> Optional[models.Model]:
        """Runs the custom training job.

        Distributed Training Support:
        If replica count = 1 then one chief replica will be provisioned. If
        replica_count > 1 the remainder will be provisioned as a worker replica pool.
        ie: replica_count = 10 will result in 1 chief and 9 workers
        All replicas have same machine_type, accelerator_type, and accelerator_count

        If training on a Vertex AI dataset, you can use one of the following split configurations:
            Data fraction splits:
            Any of ``training_fraction_split``, ``validation_fraction_split`` and
            ``test_fraction_split`` may optionally be provided, they must sum to up to 1. If
            the provided ones sum to less than 1, the remainder is assigned to sets as
            decided by Vertex AI. If none of the fractions are set, by default roughly 80%
            of data will be used for training, 10% for validation, and 10% for test.

            Data filter splits:
            Assigns input data to training, validation, and test sets
            based on the given filters, data pieces not matched by any
            filter are ignored. Currently only supported for Datasets
            containing DataItems.
            If any of the filters in this message are to match nothing, then
            they can be set as '-' (the minus sign).
            If using filter splits, all of ``training_filter_split``, ``validation_filter_split`` and
            ``test_filter_split`` must be provided.
            Supported only for unstructured Datasets.

            Predefined splits:
            Assigns input data to training, validation, and test sets based on the value of a provided key.
            If using predefined splits, ``predefined_split_column_name`` must be provided.
            Supported only for tabular Datasets.

            Timestamp splits:
            Assigns input data to training, validation, and test sets
            based on a provided timestamps. The youngest data pieces are
            assigned to training set, next to validation set, and the oldest
            to the test set.
            Supported only for tabular Datasets.

        Args:
            dataset (Union[datasets.ImageDataset,datasets.TabularDataset,datasets.TextDataset,datasets.VideoDataset]):
                Vertex AI to fit this training against. Custom training script should
                retrieve datasets through passed in environment variables uris:

                os.environ["AIP_TRAINING_DATA_URI"]
                os.environ["AIP_VALIDATION_DATA_URI"]
                os.environ["AIP_TEST_DATA_URI"]

                Additionally the dataset format is passed in as:

                os.environ["AIP_DATA_FORMAT"]
            annotation_schema_uri (str):
                Google Cloud Storage URI points to a YAML file describing
                annotation schema. The schema is defined as an OpenAPI 3.0.2
                [Schema Object](https://github.com/OAI/OpenAPI-Specification/blob/main/versions/3.0.2.md#schema-object) The schema files
                that can be used here are found in
                gs://google-cloud-aiplatform/schema/dataset/annotation/,
                note that the chosen schema must be consistent with
                ``metadata``
                of the Dataset specified by
                ``dataset_id``.

                Only Annotations that both match this schema and belong to
                DataItems not ignored by the split method are used in
                respectively training, validation or test role, depending on
                the role of the DataItem they are on.

                When used in conjunction with
                ``annotations_filter``,
                the Annotations used for training are filtered by both
                ``annotations_filter``
                and
                ``annotation_schema_uri``.
            model_display_name (str):
                If the script produces a managed Vertex AI Model. The display name of
                the Model. The name can be up to 128 characters long and can be consist
                of any UTF-8 characters.

                If not provided upon creation, the job's display_name is used.
            model_labels (Dict[str, str]):
                Optional. The labels with user-defined metadata to
                organize your Models.
                Label keys and values can be no longer than 64
                characters (Unicode codepoints), can only
                contain lowercase letters, numeric characters,
                underscores and dashes. International characters
                are allowed.
                See https://goo.gl/xmQnxf for more information
                and examples of labels.
            model_id (str):
                Optional. The ID to use for the Model produced by this job,
                which will become the final component of the model resource name.
                This value may be up to 63 characters, and valid characters
                are `[a-z0-9_-]`. The first character cannot be a number or hyphen.
            parent_model (str):
                Optional. The resource name or model ID of an existing model.
                The new model uploaded by this job will be a version of `parent_model`.

                Only set this field when training a new version of an existing model.
            is_default_version (bool):
                Optional. When set to True, the newly uploaded model version will
                automatically have alias "default" included. Subsequent uses of
                the model produced by this job without a version specified will
                use this "default" version.

                When set to False, the "default" alias will not be moved.
                Actions targeting the model version produced by this job will need
                to specifically reference this version by ID or alias.

                New model uploads, i.e. version 1, will always be "default" aliased.
            model_version_aliases (Sequence[str]):
                Optional. User provided version aliases so that the model version
                uploaded by this job can be referenced via alias instead of
                auto-generated version ID. A default version alias will be created
                for the first version of the model.

                The format is [a-z][a-zA-Z0-9-]{0,126}[a-z0-9]
            model_version_description (str):
               Optional. The description of the model version being uploaded by this job.
            base_output_dir (str):
                GCS output directory of job. If not provided a
                timestamped directory in the staging directory will be used.

                Vertex AI sets the following environment variables when it runs your training code:

                -  AIP_MODEL_DIR: a Cloud Storage URI of a directory intended for saving model artifacts, i.e. <base_output_dir>/model/
                -  AIP_CHECKPOINT_DIR: a Cloud Storage URI of a directory intended for saving checkpoints, i.e. <base_output_dir>/checkpoints/
                -  AIP_TENSORBOARD_LOG_DIR: a Cloud Storage URI of a directory intended for saving TensorBoard logs, i.e. <base_output_dir>/logs/

            service_account (str):
                Specifies the service account for workload run-as account.
                Users submitting jobs must have act-as permission on this run-as account.
            network (str):
                The full name of the Compute Engine network to which the job
                should be peered. For example, projects/12345/global/networks/myVPC.
                Private services access must already be configured for the network.
                If left unspecified, the network set in aiplatform.init will be used.
                Otherwise, the job is not peered with any network.
            bigquery_destination (str):
                Provide this field if `dataset` is a BigQuery dataset.
                The BigQuery project location where the training data is to
                be written to. In the given project a new dataset is created
                with name
                ``dataset_<dataset-id>_<annotation-type>_<timestamp-of-training-call>``
                where timestamp is in YYYY_MM_DDThh_mm_ss_sssZ format. All
                training input data will be written into that dataset. In
                the dataset three tables will be created, ``training``,
                ``validation`` and ``test``.

                -  AIP_DATA_FORMAT = "bigquery".
                -  AIP_TRAINING_DATA_URI ="bigquery_destination.dataset_*.training"
                -  AIP_VALIDATION_DATA_URI = "bigquery_destination.dataset_*.validation"
                -  AIP_TEST_DATA_URI = "bigquery_destination.dataset_*.test"
            args (List[Unions[str, int, float]]):
                Command line arguments to be passed to the Python script.
            environment_variables (Dict[str, str]):
                Environment variables to be passed to the container.
                Should be a dictionary where keys are environment variable names
                and values are environment variable values for those names.
                At most 10 environment variables can be specified.
                The Name of the environment variable must be unique.

                environment_variables = {
                    'MY_KEY': 'MY_VALUE'
                }
            replica_count (int):
                The number of worker replicas. If replica count = 1 then one chief
                replica will be provisioned. If replica_count > 1 the remainder will be
                provisioned as a worker replica pool.
            machine_type (str):
                The type of machine to use for training.
            accelerator_type (str):
                Hardware accelerator type. One of ACCELERATOR_TYPE_UNSPECIFIED,
                NVIDIA_TESLA_K80, NVIDIA_TESLA_P100, NVIDIA_TESLA_V100, NVIDIA_TESLA_P4,
                NVIDIA_TESLA_T4
            accelerator_count (int):
                The number of accelerators to attach to a worker replica.
            boot_disk_type (str):
                Type of the boot disk, default is `pd-ssd`.
                Valid values: `pd-ssd` (Persistent Disk Solid State Drive) or
                `pd-standard` (Persistent Disk Hard Disk Drive).
            boot_disk_size_gb (int):
                Size in GB of the boot disk, default is 100GB.
                boot disk size must be within the range of [100, 64000].
            reduction_server_replica_count (int):
                The number of reduction server replicas, default is 0.
            reduction_server_machine_type (str):
                Optional. The type of machine to use for reduction server.
            reduction_server_container_uri (str):
                Optional. The Uri of the reduction server container image.
                See details: https://cloud.google.com/vertex-ai/docs/training/distributed-training#reduce_training_time_with_reduction_server
            training_fraction_split (float):
                Optional. The fraction of the input data that is to be used to train
                the Model. This is ignored if Dataset is not provided.
            validation_fraction_split (float):
                Optional. The fraction of the input data that is to be used to validate
                the Model. This is ignored if Dataset is not provided.
            test_fraction_split (float):
                Optional. The fraction of the input data that is to be used to evaluate
                the Model. This is ignored if Dataset is not provided.
            training_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to train the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            validation_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to validate the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            test_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to test the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            predefined_split_column_name (str):
                Optional. The key is a name of one of the Dataset's data
                columns. The value of the key (either the label's value or
                value in the column) must be one of {``training``,
                ``validation``, ``test``}, and it defines to which set the
                given piece of data is assigned. If for a piece of data the
                key is not present or has an invalid value, that piece is
                ignored by the pipeline.

                Supported only for tabular and time series Datasets.
            timestamp_split_column_name (str):
                Optional. The key is a name of one of the Dataset's data
                columns. The value of the key values of the key (the values in
                the column) must be in RFC 3339 `date-time` format, where
                `time-offset` = `"Z"` (e.g. 1985-04-12T23:20:50.52Z). If for a
                piece of data the key is not present or has an invalid value,
                that piece is ignored by the pipeline.

                Supported only for tabular and time series Datasets.
            timeout (int):
                The maximum job running time in seconds. The default is 7 days.
            restart_job_on_worker_restart (bool):
                Restarts the entire CustomJob if a worker
                gets restarted. This feature can be used by
                distributed training jobs that are not resilient
                to workers leaving and joining a job.
            enable_web_access (bool):
                Whether you want Vertex AI to enable interactive shell access
                to training containers.
                https://cloud.google.com/vertex-ai/docs/training/monitor-debug-interactive-shell
            enable_dashboard_access (bool):
                Whether you want Vertex AI to enable access to the customized dashboard
                to training containers.
            tensorboard (str):
                Optional. The name of a Vertex AI
                [Tensorboard][google.cloud.aiplatform.v1beta1.Tensorboard]
                resource to which this CustomJob will upload Tensorboard
                logs. Format:
                ``projects/{project}/locations/{location}/tensorboards/{tensorboard}``

                The training script should write Tensorboard to following Vertex AI environment
                variable:

                AIP_TENSORBOARD_LOG_DIR

                `service_account` is required with provided `tensorboard`.
                For more information on configuring your service account please visit:
                https://cloud.google.com/vertex-ai/docs/experiments/tensorboard-training
            sync (bool):
                Whether to execute this method synchronously. If False, this method
                will be executed in concurrent Future and any downstream object will
                be immediately returned and synced when the Future has completed.
            create_request_timeout (float):
                Optional. The timeout for the create request in seconds.
            disable_retries (bool):
                Indicates if the job should retry for internal errors after the
                job starts running. If True, overrides
                `restart_job_on_worker_restart` to False.
            persistent_resource_id (str):
                Optional. The ID of the PersistentResource in the same Project
                and Location. If this is specified, the job will be run on
                existing machines held by the PersistentResource instead of
                on-demand short-live machines. The network, CMEK, and node pool
                configs on the job should be consistent with those on the
                PersistentResource, otherwise, the job will be rejected.
            tpu_topology (str):
                Optional. Specifies the tpu topology to be used for
                TPU training job. This field is required for TPU v5 versions. For
                details on the TPU topology, refer to
                https://cloud.google.com/tpu/docs/v5e#tpu-v5e-config. The topology
                must be a supported value for the TPU machine type.

        Returns:
            model: The trained Vertex AI Model resource or None if training did not
                produce a Vertex AI Model.

        Raises:
            RuntimeError: If Training job has already been run, staging_bucket has not
                been set, or model_display_name was provided but required arguments
                were not provided in constructor.
        """
        network = network or initializer.global_config.network
        service_account = service_account or initializer.global_config.service_account

        worker_pool_specs, managed_model = self._prepare_and_validate_run(
            model_display_name=model_display_name,
            model_labels=model_labels,
            replica_count=replica_count,
            machine_type=machine_type,
            accelerator_count=accelerator_count,
            accelerator_type=accelerator_type,
            boot_disk_type=boot_disk_type,
            boot_disk_size_gb=boot_disk_size_gb,
            reduction_server_replica_count=reduction_server_replica_count,
            reduction_server_machine_type=reduction_server_machine_type,
            tpu_topology=tpu_topology,
        )

        return self._run(
            dataset=dataset,
            annotation_schema_uri=annotation_schema_uri,
            worker_pool_specs=worker_pool_specs,
            managed_model=managed_model,
            model_id=model_id,
            parent_model=parent_model,
            is_default_version=is_default_version,
            model_version_aliases=model_version_aliases,
            model_version_description=model_version_description,
            args=args,
            environment_variables=environment_variables,
            base_output_dir=base_output_dir,
            service_account=service_account,
            network=network,
            bigquery_destination=bigquery_destination,
            training_fraction_split=training_fraction_split,
            validation_fraction_split=validation_fraction_split,
            test_fraction_split=test_fraction_split,
            training_filter_split=training_filter_split,
            validation_filter_split=validation_filter_split,
            test_filter_split=test_filter_split,
            predefined_split_column_name=predefined_split_column_name,
            timestamp_split_column_name=timestamp_split_column_name,
            timeout=timeout,
            restart_job_on_worker_restart=restart_job_on_worker_restart,
            enable_web_access=enable_web_access,
            enable_dashboard_access=enable_dashboard_access,
            tensorboard=tensorboard,
            reduction_server_container_uri=reduction_server_container_uri
            if reduction_server_replica_count > 0
            else None,
            sync=sync,
            create_request_timeout=create_request_timeout,
            disable_retries=disable_retries,
            persistent_resource_id=persistent_resource_id,
        )

    def submit(
        self,
        dataset: Optional[
            Union[
                datasets.ImageDataset,
                datasets.TabularDataset,
                datasets.TextDataset,
                datasets.VideoDataset,
            ]
        ] = None,
        annotation_schema_uri: Optional[str] = None,
        model_display_name: Optional[str] = None,
        model_labels: Optional[Dict[str, str]] = None,
        model_id: Optional[str] = None,
        parent_model: Optional[str] = None,
        is_default_version: Optional[bool] = True,
        model_version_aliases: Optional[Sequence[str]] = None,
        model_version_description: Optional[str] = None,
        base_output_dir: Optional[str] = None,
        service_account: Optional[str] = None,
        network: Optional[str] = None,
        bigquery_destination: Optional[str] = None,
        args: Optional[List[Union[str, float, int]]] = None,
        environment_variables: Optional[Dict[str, str]] = None,
        replica_count: int = 1,
        machine_type: str = "n1-standard-4",
        accelerator_type: str = "ACCELERATOR_TYPE_UNSPECIFIED",
        accelerator_count: int = 0,
        boot_disk_type: str = "pd-ssd",
        boot_disk_size_gb: int = 100,
        reduction_server_replica_count: int = 0,
        reduction_server_machine_type: Optional[str] = None,
        reduction_server_container_uri: Optional[str] = None,
        training_fraction_split: Optional[float] = None,
        validation_fraction_split: Optional[float] = None,
        test_fraction_split: Optional[float] = None,
        training_filter_split: Optional[str] = None,
        validation_filter_split: Optional[str] = None,
        test_filter_split: Optional[str] = None,
        predefined_split_column_name: Optional[str] = None,
        timestamp_split_column_name: Optional[str] = None,
        timeout: Optional[int] = None,
        restart_job_on_worker_restart: bool = False,
        enable_web_access: bool = False,
        enable_dashboard_access: bool = False,
        tensorboard: Optional[str] = None,
        sync=True,
        create_request_timeout: Optional[float] = None,
        disable_retries: bool = False,
        persistent_resource_id: Optional[str] = None,
        tpu_topology: Optional[str] = None,
    ) -> Optional[models.Model]:
        """Submits the custom training job without blocking until completion.

        Distributed Training Support:
        If replica count = 1 then one chief replica will be provisioned. If
        replica_count > 1 the remainder will be provisioned as a worker replica pool.
        ie: replica_count = 10 will result in 1 chief and 9 workers
        All replicas have same machine_type, accelerator_type, and accelerator_count

        If training on a Vertex AI dataset, you can use one of the following split configurations:
            Data fraction splits:
            Any of ``training_fraction_split``, ``validation_fraction_split`` and
            ``test_fraction_split`` may optionally be provided, they must sum to up to 1. If
            the provided ones sum to less than 1, the remainder is assigned to sets as
            decided by Vertex AI. If none of the fractions are set, by default roughly 80%
            of data will be used for training, 10% for validation, and 10% for test.

            Data filter splits:
            Assigns input data to training, validation, and test sets
            based on the given filters, data pieces not matched by any
            filter are ignored. Currently only supported for Datasets
            containing DataItems.
            If any of the filters in this message are to match nothing, then
            they can be set as '-' (the minus sign).
            If using filter splits, all of ``training_filter_split``, ``validation_filter_split`` and
            ``test_filter_split`` must be provided.
            Supported only for unstructured Datasets.

            Predefined splits:
            Assigns input data to training, validation, and test sets based on the value of a provided key.
            If using predefined splits, ``predefined_split_column_name`` must be provided.
            Supported only for tabular Datasets.

            Timestamp splits:
            Assigns input data to training, validation, and test sets
            based on a provided timestamps. The youngest data pieces are
            assigned to training set, next to validation set, and the oldest
            to the test set.
            Supported only for tabular Datasets.

        Args:
            dataset (Union[datasets.ImageDataset,datasets.TabularDataset,datasets.TextDataset,datasets.VideoDataset]):
                Vertex AI to fit this training against. Custom training script should
                retrieve datasets through passed in environment variables uris:

                os.environ["AIP_TRAINING_DATA_URI"]
                os.environ["AIP_VALIDATION_DATA_URI"]
                os.environ["AIP_TEST_DATA_URI"]

                Additionally the dataset format is passed in as:

                os.environ["AIP_DATA_FORMAT"]
            annotation_schema_uri (str):
                Google Cloud Storage URI points to a YAML file describing
                annotation schema. The schema is defined as an OpenAPI 3.0.2
                [Schema Object](https://github.com/OAI/OpenAPI-Specification/blob/main/versions/3.0.2.md#schema-object) The schema files
                that can be used here are found in
                gs://google-cloud-aiplatform/schema/dataset/annotation/,
                note that the chosen schema must be consistent with
                ``metadata``
                of the Dataset specified by
                ``dataset_id``.

                Only Annotations that both match this schema and belong to
                DataItems not ignored by the split method are used in
                respectively training, validation or test role, depending on
                the role of the DataItem they are on.

                When used in conjunction with
                ``annotations_filter``,
                the Annotations used for training are filtered by both
                ``annotations_filter``
                and
                ``annotation_schema_uri``.
            model_display_name (str):
                If the script produces a managed Vertex AI Model. The display name of
                the Model. The name can be up to 128 characters long and can be consist
                of any UTF-8 characters.

                If not provided upon creation, the job's display_name is used.
            model_labels (Dict[str, str]):
                Optional. The labels with user-defined metadata to
                organize your Models.
                Label keys and values can be no longer than 64
                characters (Unicode codepoints), can only
                contain lowercase letters, numeric characters,
                underscores and dashes. International characters
                are allowed.
                See https://goo.gl/xmQnxf for more information
                and examples of labels.
            model_id (str):
                Optional. The ID to use for the Model produced by this job,
                which will become the final component of the model resource name.
                This value may be up to 63 characters, and valid characters
                are `[a-z0-9_-]`. The first character cannot be a number or hyphen.
            parent_model (str):
                Optional. The resource name or model ID of an existing model.
                The new model uploaded by this job will be a version of `parent_model`.

                Only set this field when training a new version of an existing model.
            is_default_version (bool):
                Optional. When set to True, the newly uploaded model version will
                automatically have alias "default" included. Subsequent uses of
                the model produced by this job without a version specified will
                use this "default" version.

                When set to False, the "default" alias will not be moved.
                Actions targeting the model version produced by this job will need
                to specifically reference this version by ID or alias.

                New model uploads, i.e. version 1, will always be "default" aliased.
            model_version_aliases (Sequence[str]):
                Optional. User provided version aliases so that the model version
                uploaded by this job can be referenced via alias instead of
                auto-generated version ID. A default version alias will be created
                for the first version of the model.

                The format is [a-z][a-zA-Z0-9-]{0,126}[a-z0-9]
            model_version_description (str):
               Optional. The description of the model version being uploaded by this job.
            base_output_dir (str):
                GCS output directory of job. If not provided a
                timestamped directory in the staging directory will be used.

                Vertex AI sets the following environment variables when it runs your training code:

                -  AIP_MODEL_DIR: a Cloud Storage URI of a directory intended for saving model artifacts, i.e. <base_output_dir>/model/
                -  AIP_CHECKPOINT_DIR: a Cloud Storage URI of a directory intended for saving checkpoints, i.e. <base_output_dir>/checkpoints/
                -  AIP_TENSORBOARD_LOG_DIR: a Cloud Storage URI of a directory intended for saving TensorBoard logs, i.e. <base_output_dir>/logs/

            service_account (str):
                Specifies the service account for workload run-as account.
                Users submitting jobs must have act-as permission on this run-as account.
            network (str):
                The full name of the Compute Engine network to which the job
                should be peered. For example, projects/12345/global/networks/myVPC.
                Private services access must already be configured for the network.
                If left unspecified, the network set in aiplatform.init will be used.
                Otherwise, the job is not peered with any network.
            bigquery_destination (str):
                Provide this field if `dataset` is a BigQuery dataset.
                The BigQuery project location where the training data is to
                be written to. In the given project a new dataset is created
                with name
                ``dataset_<dataset-id>_<annotation-type>_<timestamp-of-training-call>``
                where timestamp is in YYYY_MM_DDThh_mm_ss_sssZ format. All
                training input data will be written into that dataset. In
                the dataset three tables will be created, ``training``,
                ``validation`` and ``test``.

                -  AIP_DATA_FORMAT = "bigquery".
                -  AIP_TRAINING_DATA_URI ="bigquery_destination.dataset_*.training"
                -  AIP_VALIDATION_DATA_URI = "bigquery_destination.dataset_*.validation"
                -  AIP_TEST_DATA_URI = "bigquery_destination.dataset_*.test"
            args (List[Unions[str, int, float]]):
                Command line arguments to be passed to the Python script.
            environment_variables (Dict[str, str]):
                Environment variables to be passed to the container.
                Should be a dictionary where keys are environment variable names
                and values are environment variable values for those names.
                At most 10 environment variables can be specified.
                The Name of the environment variable must be unique.

                environment_variables = {
                    'MY_KEY': 'MY_VALUE'
                }
            replica_count (int):
                The number of worker replicas. If replica count = 1 then one chief
                replica will be provisioned. If replica_count > 1 the remainder will be
                provisioned as a worker replica pool.
            machine_type (str):
                The type of machine to use for training.
            accelerator_type (str):
                Hardware accelerator type. One of ACCELERATOR_TYPE_UNSPECIFIED,
                NVIDIA_TESLA_K80, NVIDIA_TESLA_P100, NVIDIA_TESLA_V100, NVIDIA_TESLA_P4,
                NVIDIA_TESLA_T4
            accelerator_count (int):
                The number of accelerators to attach to a worker replica.
            boot_disk_type (str):
                Type of the boot disk, default is `pd-ssd`.
                Valid values: `pd-ssd` (Persistent Disk Solid State Drive) or
                `pd-standard` (Persistent Disk Hard Disk Drive).
            boot_disk_size_gb (int):
                Size in GB of the boot disk, default is 100GB.
                boot disk size must be within the range of [100, 64000].
            reduction_server_replica_count (int):
                The number of reduction server replicas, default is 0.
            reduction_server_machine_type (str):
                Optional. The type of machine to use for reduction server.
            reduction_server_container_uri (str):
                Optional. The Uri of the reduction server container image.
                See details: https://cloud.google.com/vertex-ai/docs/training/distributed-training#reduce_training_time_with_reduction_server
            training_fraction_split (float):
                Optional. The fraction of the input data that is to be used to train
                the Model. This is ignored if Dataset is not provided.
            validation_fraction_split (float):
                Optional. The fraction of the input data that is to be used to validate
                the Model. This is ignored if Dataset is not provided.
            test_fraction_split (float):
                Optional. The fraction of the input data that is to be used to evaluate
                the Model. This is ignored if Dataset is not provided.
            training_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to train the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            validation_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to validate the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            test_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to test the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            predefined_split_column_name (str):
                Optional. The key is a name of one of the Dataset's data
                columns. The value of the key (either the label's value or
                value in the column) must be one of {``training``,
                ``validation``, ``test``}, and it defines to which set the
                given piece of data is assigned. If for a piece of data the
                key is not present or has an invalid value, that piece is
                ignored by the pipeline.

                Supported only for tabular and time series Datasets.
            timestamp_split_column_name (str):
                Optional. The key is a name of one of the Dataset's data
                columns. The value of the key values of the key (the values in
                the column) must be in RFC 3339 `date-time` format, where
                `time-offset` = `"Z"` (e.g. 1985-04-12T23:20:50.52Z). If for a
                piece of data the key is not present or has an invalid value,
                that piece is ignored by the pipeline.

                Supported only for tabular and time series Datasets.
            timeout (int):
                The maximum job running time in seconds. The default is 7 days.
            restart_job_on_worker_restart (bool):
                Restarts the entire CustomJob if a worker
                gets restarted. This feature can be used by
                distributed training jobs that are not resilient
                to workers leaving and joining a job.
            enable_web_access (bool):
                Whether you want Vertex AI to enable interactive shell access
                to training containers.
                https://cloud.google.com/vertex-ai/docs/training/monitor-debug-interactive-shell
            enable_dashboard_access (bool):
                Whether you want Vertex AI to enable access to the customized dashboard
                to training containers.
            tensorboard (str):
                Optional. The name of a Vertex AI
                [Tensorboard][google.cloud.aiplatform.v1beta1.Tensorboard]
                resource to which this CustomJob will upload Tensorboard
                logs. Format:
                ``projects/{project}/locations/{location}/tensorboards/{tensorboard}``

                The training script should write Tensorboard to following Vertex AI environment
                variable:

                AIP_TENSORBOARD_LOG_DIR

                `service_account` is required with provided `tensorboard`.
                For more information on configuring your service account please visit:
                https://cloud.google.com/vertex-ai/docs/experiments/tensorboard-training
            sync (bool):
                Whether to execute this method synchronously. If False, this method
                will be executed in concurrent Future and any downstream object will
                be immediately returned and synced when the Future has completed.
            create_request_timeout (float):
                Optional. The timeout for the create request in seconds.
            disable_retries (bool):
                Indicates if the job should retry for internal errors after the
                job starts running. If True, overrides
                `restart_job_on_worker_restart` to False.
            persistent_resource_id (str):
                Optional. The ID of the PersistentResource in the same Project
                and Location. If this is specified, the job will be run on
                existing machines held by the PersistentResource instead of
                on-demand short-live machines. The network, CMEK, and node pool
                configs on the job should be consistent with those on the
                PersistentResource, otherwise, the job will be rejected.
            tpu_topology (str):
                Optional. Specifies the tpu topology to be used for
                TPU training job. This field is required for TPU v5 versions. For
                details on the TPU topology, refer to
                https://cloud.google.com/tpu/docs/v5e#tpu-v5e-config. The topology
                must be a supported value for the TPU machine type.

        Returns:
            model: The trained Vertex AI Model resource or None if training did not
                produce a Vertex AI Model.

        Raises:
            RuntimeError: If Training job has already been run, staging_bucket has not
                been set, or model_display_name was provided but required arguments
                were not provided in constructor.
        """
        network = network or initializer.global_config.network

        worker_pool_specs, managed_model = self._prepare_and_validate_run(
            model_display_name=model_display_name,
            model_labels=model_labels,
            replica_count=replica_count,
            machine_type=machine_type,
            accelerator_count=accelerator_count,
            accelerator_type=accelerator_type,
            boot_disk_type=boot_disk_type,
            boot_disk_size_gb=boot_disk_size_gb,
            reduction_server_replica_count=reduction_server_replica_count,
            reduction_server_machine_type=reduction_server_machine_type,
            tpu_topology=tpu_topology,
        )

        return self._run(
            dataset=dataset,
            annotation_schema_uri=annotation_schema_uri,
            worker_pool_specs=worker_pool_specs,
            managed_model=managed_model,
            model_id=model_id,
            parent_model=parent_model,
            is_default_version=is_default_version,
            model_version_aliases=model_version_aliases,
            model_version_description=model_version_description,
            args=args,
            environment_variables=environment_variables,
            base_output_dir=base_output_dir,
            service_account=service_account,
            network=network,
            bigquery_destination=bigquery_destination,
            training_fraction_split=training_fraction_split,
            validation_fraction_split=validation_fraction_split,
            test_fraction_split=test_fraction_split,
            training_filter_split=training_filter_split,
            validation_filter_split=validation_filter_split,
            test_filter_split=test_filter_split,
            predefined_split_column_name=predefined_split_column_name,
            timestamp_split_column_name=timestamp_split_column_name,
            timeout=timeout,
            restart_job_on_worker_restart=restart_job_on_worker_restart,
            enable_web_access=enable_web_access,
            enable_dashboard_access=enable_dashboard_access,
            tensorboard=tensorboard,
            reduction_server_container_uri=reduction_server_container_uri
            if reduction_server_replica_count > 0
            else None,
            sync=sync,
            create_request_timeout=create_request_timeout,
            block=False,
            disable_retries=disable_retries,
            persistent_resource_id=persistent_resource_id,
        )

    @base.optional_sync(construct_object_on_arg="managed_model")
    def _run(
        self,
        dataset: Optional[
            Union[
                datasets.ImageDataset,
                datasets.TabularDataset,
                datasets.TextDataset,
                datasets.VideoDataset,
            ]
        ],
        annotation_schema_uri: Optional[str],
        worker_pool_specs: worker_spec_utils._DistributedTrainingSpec,
        managed_model: Optional[gca_model.Model] = None,
        model_id: Optional[str] = None,
        parent_model: Optional[str] = None,
        is_default_version: Optional[bool] = True,
        model_version_aliases: Optional[Sequence[str]] = None,
        model_version_description: Optional[str] = None,
        args: Optional[List[Union[str, float, int]]] = None,
        environment_variables: Optional[Dict[str, str]] = None,
        base_output_dir: Optional[str] = None,
        service_account: Optional[str] = None,
        network: Optional[str] = None,
        bigquery_destination: Optional[str] = None,
        training_fraction_split: Optional[float] = None,
        validation_fraction_split: Optional[float] = None,
        test_fraction_split: Optional[float] = None,
        training_filter_split: Optional[str] = None,
        validation_filter_split: Optional[str] = None,
        test_filter_split: Optional[str] = None,
        predefined_split_column_name: Optional[str] = None,
        timestamp_split_column_name: Optional[str] = None,
        timeout: Optional[int] = None,
        restart_job_on_worker_restart: bool = False,
        enable_web_access: bool = False,
        enable_dashboard_access: bool = False,
        tensorboard: Optional[str] = None,
        reduction_server_container_uri: Optional[str] = None,
        sync=True,
        create_request_timeout: Optional[float] = None,
        block: Optional[bool] = True,
        disable_retries: bool = False,
        persistent_resource_id: Optional[str] = None,
    ) -> Optional[models.Model]:
        """Packages local script and launches training_job.
        Args:
            dataset (
                Union[
                    datasets.ImageDataset,
                    datasets.TabularDataset,
                    datasets.TextDataset,
                    datasets.VideoDataset,
                ]
            ):
                Vertex AI to fit this training against.
            annotation_schema_uri (str):
                Google Cloud Storage URI points to a YAML file describing
                annotation schema.
            worker_pools_spec (worker_spec_utils._DistributedTrainingSpec):
                Worker pools pecs required to run job.
            managed_model (gca_model.Model):
                Model proto if this script produces a Managed Model.
            model_id (str):
                Optional. The ID to use for the Model produced by this job,
                which will become the final component of the model resource name.
                This value may be up to 63 characters, and valid characters
                are `[a-z0-9_-]`. The first character cannot be a number or hyphen.
            parent_model (str):
                Optional. The resource name or model ID of an existing model.
                The new model uploaded by this job will be a version of `parent_model`.

                Only set this field when training a new version of an existing model.
            is_default_version (bool):
                Optional. When set to True, the newly uploaded model version will
                automatically have alias "default" included. Subsequent uses of
                the model produced by this job without a version specified will
                use this "default" version.

                When set to False, the "default" alias will not be moved.
                Actions targeting the model version produced by this job will need
                to specifically reference this version by ID or alias.

                New model uploads, i.e. version 1, will always be "default" aliased.
            model_version_aliases (Sequence[str]):
                Optional. User provided version aliases so that the model version
                uploaded by this job can be referenced via alias instead of
                auto-generated version ID. A default version alias will be created
                for the first version of the model.

                The format is [a-z][a-zA-Z0-9-]{0,126}[a-z0-9]
            model_version_description (str):
               Optional. The description of the model version being uploaded by this job.
            args (List[Unions[str, int, float]]):
                Command line arguments to be passed to the Python script.
            environment_variables (Dict[str, str]):
                Environment variables to be passed to the container.
                Should be a dictionary where keys are environment variable names
                and values are environment variable values for those names.
                At most 10 environment variables can be specified.
                The Name of the environment variable must be unique.

                environment_variables = {
                    'MY_KEY': 'MY_VALUE'
                }
            base_output_dir (str):
                GCS output directory of job. If not provided a
                timestamped directory in the staging directory will be used.

                Vertex AI sets the following environment variables when it runs your training code:

                -  AIP_MODEL_DIR: a Cloud Storage URI of a directory intended for saving model artifacts, i.e. <base_output_dir>/model/
                -  AIP_CHECKPOINT_DIR: a Cloud Storage URI of a directory intended for saving checkpoints, i.e. <base_output_dir>/checkpoints/
                -  AIP_TENSORBOARD_LOG_DIR: a Cloud Storage URI of a directory intended for saving TensorBoard logs, i.e. <base_output_dir>/logs/

            service_account (str):
                Specifies the service account for workload run-as account.
                Users submitting jobs must have act-as permission on this run-as account.
            network (str):
                The full name of the Compute Engine network to which the job
                should be peered. For example, projects/12345/global/networks/myVPC.
                Private services access must already be configured for the network.
                If left unspecified, the job is not peered with any network.
            timeout (int):
                The maximum job running time in seconds. The default is 7 days.
            restart_job_on_worker_restart (bool):
                Restarts the entire CustomJob if a worker
                gets restarted. This feature can be used by
                distributed training jobs that are not resilient
                to workers leaving and joining a job.
            bigquery_destination (str):
                The BigQuery project location where the training data is to
                be written to. In the given project a new dataset is created
                with name
                ``dataset_<dataset-id>_<annotation-type>_<timestamp-of-training-call>``
                where timestamp is in YYYY_MM_DDThh_mm_ss_sssZ format. All
                training input data will be written into that dataset. In
                the dataset three tables will be created, ``training``,
                ``validation`` and ``test``.

                -  AIP_DATA_FORMAT = "bigquery".
                -  AIP_TRAINING_DATA_URI ="bigquery_destination.dataset_*.training"
                -  AIP_VALIDATION_DATA_URI = "bigquery_destination.dataset_*.validation"
                -  AIP_TEST_DATA_URI = "bigquery_destination.dataset_*.test"
            training_fraction_split (float):
                Optional. The fraction of the input data that is to be used to train
                the Model. This is ignored if Dataset is not provided.
            validation_fraction_split (float):
                Optional. The fraction of the input data that is to be used to validate
                the Model. This is ignored if Dataset is not provided.
            test_fraction_split (float):
                Optional. The fraction of the input data that is to be used to evaluate
                the Model. This is ignored if Dataset is not provided.
            training_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to train the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            validation_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to validate the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            test_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to test the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            predefined_split_column_name (str):
                Optional. The key is a name of one of the Dataset's data
                columns. The value of the key (either the label's value or
                value in the column) must be one of {``training``,
                ``validation``, ``test``}, and it defines to which set the
                given piece of data is assigned. If for a piece of data the
                key is not present or has an invalid value, that piece is
                ignored by the pipeline.

                Supported only for tabular and time series Datasets.
            timestamp_split_column_name (str):
                Optional. The key is a name of one of the Dataset's data
                columns. The value of the key values of the key (the values in
                the column) must be in RFC 3339 `date-time` format, where
                `time-offset` = `"Z"` (e.g. 1985-04-12T23:20:50.52Z). If for a
                piece of data the key is not present or has an invalid value,
                that piece is ignored by the pipeline.

                Supported only for tabular and time series Datasets.
            enable_web_access (bool):
                Whether you want Vertex AI to enable interactive shell access
                to training containers.
                https://cloud.google.com/vertex-ai/docs/training/monitor-debug-interactive-shell
            enable_dashboard_access (bool):
                Whether you want Vertex AI to enable access to the customized dashboard
                to training containers.
            tensorboard (str):
                Optional. The name of a Vertex AI
                [Tensorboard][google.cloud.aiplatform.v1beta1.Tensorboard]
                resource to which this CustomJob will upload Tensorboard
                logs. Format:
                ``projects/{project}/locations/{location}/tensorboards/{tensorboard}``

                The training script should write Tensorboard to following Vertex AI environment
                variable:

                AIP_TENSORBOARD_LOG_DIR

                `service_account` is required with provided `tensorboard`.
                For more information on configuring your service account please visit:
                https://cloud.google.com/vertex-ai/docs/experiments/tensorboard-training
            reduction_server_container_uri (str):
                Optional. The Uri of the reduction server container image.
            sync (bool):
                Whether to execute this method synchronously. If False, this method
                will be executed in concurrent Future and any downstream object will
                be immediately returned and synced when the Future has completed.
            create_request_timeout (float):
                Optional. The timeout for the create request in seconds.
            block (bool):
                Optional. If True, block until complete.
            disable_retries (bool):
                Indicates if the job should retry for internal errors after the
                job starts running. If True, overrides
                `restart_job_on_worker_restart` to False.
            persistent_resource_id (str):
                Optional. The ID of the PersistentResource in the same Project
                and Location. If this is specified, the job will be run on
                existing machines held by the PersistentResource instead of
                on-demand short-live machines. The network, CMEK, and node pool
                configs on the job should be consistent with those on the
                PersistentResource, otherwise, the job will be rejected.

        Returns:
            model: The trained Vertex AI Model resource or None if training did not
                produce a Vertex AI Model.
        """

        for spec_order, spec in enumerate(worker_pool_specs):

            if not spec:
                continue

            if (
                spec_order == worker_spec_utils._SPEC_ORDERS["server_spec"]
                and reduction_server_container_uri
            ):
                spec["container_spec"] = {
                    "image_uri": reduction_server_container_uri,
                }
            else:
                spec["containerSpec"] = {"imageUri": self._container_uri}

                if self._command:
                    spec["containerSpec"]["command"] = self._command

                if args:
                    spec["containerSpec"]["args"] = args

                if environment_variables:
                    spec["containerSpec"]["env"] = [
                        {"name": key, "value": value}
                        for key, value in environment_variables.items()
                    ]

        (
            training_task_inputs,
            base_output_dir,
        ) = self._prepare_training_task_inputs_and_output_dir(
            worker_pool_specs=worker_pool_specs,
            base_output_dir=base_output_dir,
            service_account=service_account,
            network=network,
            timeout=timeout,
            restart_job_on_worker_restart=restart_job_on_worker_restart,
            enable_web_access=enable_web_access,
            enable_dashboard_access=enable_dashboard_access,
            tensorboard=tensorboard,
            disable_retries=disable_retries,
            persistent_resource_id=persistent_resource_id,
        )

        model = self._run_job(
            training_task_definition=schema.training_job.definition.custom_task,
            training_task_inputs=training_task_inputs,
            dataset=dataset,
            annotation_schema_uri=annotation_schema_uri,
            training_fraction_split=training_fraction_split,
            validation_fraction_split=validation_fraction_split,
            test_fraction_split=test_fraction_split,
            training_filter_split=training_filter_split,
            validation_filter_split=validation_filter_split,
            test_filter_split=test_filter_split,
            predefined_split_column_name=predefined_split_column_name,
            timestamp_split_column_name=timestamp_split_column_name,
            model=managed_model,
            model_id=model_id,
            parent_model=parent_model,
            is_default_version=is_default_version,
            model_version_aliases=model_version_aliases,
            model_version_description=model_version_description,
            gcs_destination_uri_prefix=base_output_dir,
            bigquery_destination=bigquery_destination,
            create_request_timeout=create_request_timeout,
            block=block,
        )

        return model


class AutoMLTabularTrainingJob(_TrainingJob):
    _supported_training_schemas = (schema.training_job.definition.automl_tabular,)

    def __init__(
        self,
        # TODO(b/223262536): Make display_name parameter fully optional in next major release
        display_name: str,
        optimization_prediction_type: str,
        optimization_objective: Optional[str] = None,
        column_specs: Optional[Dict[str, str]] = None,
        column_transformations: Optional[List[Dict[str, Dict[str, str]]]] = None,
        optimization_objective_recall_value: Optional[float] = None,
        optimization_objective_precision_value: Optional[float] = None,
        project: Optional[str] = None,
        location: Optional[str] = None,
        credentials: Optional[auth_credentials.Credentials] = None,
        labels: Optional[Dict[str, str]] = None,
        training_encryption_spec_key_name: Optional[str] = None,
        model_encryption_spec_key_name: Optional[str] = None,
    ):
        """Constructs a AutoML Tabular Training Job.

        Example usage:

        job = training_jobs.AutoMLTabularTrainingJob(
            display_name="my_display_name",
            optimization_prediction_type="classification",
            optimization_objective="minimize-log-loss",
            column_specs={"column_1": "auto", "column_2": "numeric"},
            labels={'key': 'value'},
        )

        Args:
            display_name (str):
                Required. The user-defined name of this TrainingPipeline.
            optimization_prediction_type (str):
                The type of prediction the Model is to produce.
                "classification" - Predict one out of multiple target values is
                picked for each row.
                "regression" - Predict a value based on its relation to other values.
                This type is available only to columns that contain
                semantically numeric values, i.e. integers or floating
                point number, even if stored as e.g. strings.

            optimization_objective (str):
                Optional. Objective function the Model is to be optimized towards. The training
                task creates a Model that maximizes/minimizes the value of the objective
                function over the validation set.

                The supported optimization objectives depend on the prediction type, and
                in the case of classification also the number of distinct values in the
                target column (two distint values -> binary, 3 or more distinct values
                -> multi class).
                If the field is not set, the default objective function is used.

                Classification (binary):
                "maximize-au-roc" (default) - Maximize the area under the receiver
                                            operating characteristic (ROC) curve.
                "minimize-log-loss" - Minimize log loss.
                "maximize-au-prc" - Maximize the area under the precision-recall curve.
                "maximize-precision-at-recall" - Maximize precision for a specified
                                                recall value.
                "maximize-recall-at-precision" - Maximize recall for a specified
                                                precision value.

                Classification (multi class):
                "minimize-log-loss" (default) - Minimize log loss.

                Regression:
                "minimize-rmse" (default) - Minimize root-mean-squared error (RMSE).
                "minimize-mae" - Minimize mean-absolute error (MAE).
                "minimize-rmsle" - Minimize root-mean-squared log error (RMSLE).
            column_specs (Dict[str, str]):
                Optional. Alternative to column_transformations where the keys of the dict
                are column names and their respective values are one of
                AutoMLTabularTrainingJob.column_data_types.
                When creating transformation for BigQuery Struct column, the column
                should be flattened using "." as the delimiter. Only columns with no child
                should have a transformation.
                If an input column has no transformations on it, such a column is
                ignored by the training, except for the targetColumn, which should have
                no transformations defined on.
                Only one of column_transformations or column_specs should be passed. If none
                of column_transformations or column_specs is passed, the local credentials
                being used will try setting column_specs to "auto". To do this, the local
                credentials require read access to the GCS or BigQuery training data source.
            column_transformations (List[Dict[str, Dict[str, str]]]):
                Optional. Transformations to apply to the input columns (i.e. columns other
                than the targetColumn). Each transformation may produce multiple
                result values from the column's value, and all are used for training.
                When creating transformation for BigQuery Struct column, the column
                should be flattened using "." as the delimiter. Only columns with no child
                should have a transformation.
                If an input column has no transformations on it, such a column is
                ignored by the training, except for the targetColumn, which should have
                no transformations defined on.
                Only one of column_transformations or column_specs should be passed.
                Consider using column_specs as column_transformations will be deprecated
                eventually. If none of column_transformations or column_specs is passed,
                the local credentials being used will try setting column_transformations to
                "auto". To do this, the local credentials require read access to the GCS or
                BigQuery training data source.
            optimization_objective_recall_value (float):
                Optional. Required when maximize-precision-at-recall optimizationObjective was
                picked, represents the recall value at which the optimization is done.

                The minimum value is 0 and the maximum is 1.0.
            optimization_objective_precision_value (float):
                Optional. Required when maximize-recall-at-precision optimizationObjective was
                picked, represents the precision value at which the optimization is
                done.

                The minimum value is 0 and the maximum is 1.0.
            project (str):
                Optional. Project to run training in. Overrides project set in aiplatform.init.
            location (str):
                Optional. Location to run training in. Overrides location set in aiplatform.init.
            credentials (auth_credentials.Credentials):
                Optional. Custom credentials to use to run call training service. Overrides
                credentials set in aiplatform.init.
            labels (Dict[str, str]):
                Optional. The labels with user-defined metadata to
                organize TrainingPipelines.
                Label keys and values can be no longer than 64
                characters (Unicode codepoints), can only
                contain lowercase letters, numeric characters,
                underscores and dashes. International characters
                are allowed.
                See https://goo.gl/xmQnxf for more information
                and examples of labels.
            training_encryption_spec_key_name (Optional[str]):
                Optional. The Cloud KMS resource identifier of the customer
                managed encryption key used to protect the training pipeline. Has the
                form:
                ``projects/my-project/locations/my-region/keyRings/my-kr/cryptoKeys/my-key``.
                The key needs to be in the same region as where the compute
                resource is created.

                If set, this TrainingPipeline will be secured by this key.

                Note: Model trained by this TrainingPipeline is also secured
                by this key if ``model_to_upload`` is not set separately.

                Overrides encryption_spec_key_name set in aiplatform.init.
            model_encryption_spec_key_name (Optional[str]):
                Optional. The Cloud KMS resource identifier of the customer
                managed encryption key used to protect the model. Has the
                form:
                ``projects/my-project/locations/my-region/keyRings/my-kr/cryptoKeys/my-key``.
                The key needs to be in the same region as where the compute
                resource is created.

                If set, the trained Model will be secured by this key.

                Overrides encryption_spec_key_name set in aiplatform.init.

        Raises:
            ValueError: If both column_transformations and column_specs were provided.
        """
        if not display_name:
            display_name = self.__class__._generate_display_name()
        super().__init__(
            display_name=display_name,
            project=project,
            location=location,
            credentials=credentials,
            labels=labels,
            training_encryption_spec_key_name=training_encryption_spec_key_name,
            model_encryption_spec_key_name=model_encryption_spec_key_name,
        )

        self._column_transformations = (
            column_transformations_utils.validate_and_get_column_transformations(
                column_specs, column_transformations
            )
        )

        self._optimization_objective = optimization_objective
        self._optimization_prediction_type = optimization_prediction_type
        self._optimization_objective_recall_value = optimization_objective_recall_value
        self._optimization_objective_precision_value = (
            optimization_objective_precision_value
        )

        self._additional_experiments = []

    def run(
        self,
        dataset: datasets.TabularDataset,
        target_column: str,
        training_fraction_split: Optional[float] = None,
        validation_fraction_split: Optional[float] = None,
        test_fraction_split: Optional[float] = None,
        predefined_split_column_name: Optional[str] = None,
        timestamp_split_column_name: Optional[str] = None,
        weight_column: Optional[str] = None,
        budget_milli_node_hours: int = 1000,
        model_display_name: Optional[str] = None,
        model_labels: Optional[Dict[str, str]] = None,
        model_id: Optional[str] = None,
        parent_model: Optional[str] = None,
        is_default_version: Optional[bool] = True,
        model_version_aliases: Optional[Sequence[str]] = None,
        model_version_description: Optional[str] = None,
        disable_early_stopping: bool = False,
        export_evaluated_data_items: bool = False,
        export_evaluated_data_items_bigquery_destination_uri: Optional[str] = None,
        export_evaluated_data_items_override_destination: bool = False,
        additional_experiments: Optional[List[str]] = None,
        sync: bool = True,
        create_request_timeout: Optional[float] = None,
    ) -> models.Model:
        """Runs the training job and returns a model.

        If training on a Vertex AI dataset, you can use one of the following split configurations:
            Data fraction splits:
            Any of ``training_fraction_split``, ``validation_fraction_split`` and
            ``test_fraction_split`` may optionally be provided, they must sum to up to 1. If
            the provided ones sum to less than 1, the remainder is assigned to sets as
            decided by Vertex AI. If none of the fractions are set, by default roughly 80%
            of data will be used for training, 10% for validation, and 10% for test.

            Predefined splits:
            Assigns input data to training, validation, and test sets based on the value of a provided key.
            If using predefined splits, ``predefined_split_column_name`` must be provided.
            Supported only for tabular Datasets.

            Timestamp splits:
            Assigns input data to training, validation, and test sets
            based on a provided timestamps. The youngest data pieces are
            assigned to training set, next to validation set, and the oldest
            to the test set.
            Supported only for tabular Datasets.

        Args:
            dataset (datasets.TabularDataset):
                Required. The dataset within the same Project from which data will be used to train the Model. The
                Dataset must use schema compatible with Model being trained,
                and what is compatible should be described in the used
                TrainingPipeline's [training_task_definition]
                [google.cloud.aiplatform.v1beta1.TrainingPipeline.training_task_definition].
                For tabular Datasets, all their data is exported to
                training, to pick and choose from.
            target_column (str):
                Required. The name of the column values of which the Model is to predict.
            training_fraction_split (float):
                Optional. The fraction of the input data that is to be used to train
                the Model. This is ignored if Dataset is not provided.
            validation_fraction_split (float):
                Optional. The fraction of the input data that is to be used to validate
                the Model. This is ignored if Dataset is not provided.
            test_fraction_split (float):
                Optional. The fraction of the input data that is to be used to evaluate
                the Model. This is ignored if Dataset is not provided.
            predefined_split_column_name (str):
                Optional. The key is a name of one of the Dataset's data
                columns. The value of the key (either the label's value or
                value in the column) must be one of {``training``,
                ``validation``, ``test``}, and it defines to which set the
                given piece of data is assigned. If for a piece of data the
                key is not present or has an invalid value, that piece is
                ignored by the pipeline.

                Supported only for tabular and time series Datasets.
            timestamp_split_column_name (str):
                Optional. The key is a name of one of the Dataset's data
                columns. The value of the key values of the key (the values in
                the column) must be in RFC 3339 `date-time` format, where
                `time-offset` = `"Z"` (e.g. 1985-04-12T23:20:50.52Z). If for a
                piece of data the key is not present or has an invalid value,
                that piece is ignored by the pipeline.
                Supported only for tabular and time series Datasets.
                This parameter must be used with training_fraction_split,
                validation_fraction_split, and test_fraction_split.
            weight_column (str):
                Optional. Name of the column that should be used as the weight column.
                Higher values in this column give more importance to the row
                during Model training. The column must have numeric values between 0 and
                10000 inclusively, and 0 value means that the row is ignored.
                If the weight column field is not set, then all rows are assumed to have
                equal weight of 1.
            budget_milli_node_hours (int):
                Optional. The train budget of creating this Model, expressed in milli node
                hours i.e. 1,000 value in this field means 1 node hour.
                The training cost of the model will not exceed this budget. The final
                cost will be attempted to be close to the budget, though may end up
                being (even) noticeably smaller - at the backend's discretion. This
                especially may happen when further model training ceases to provide
                any improvements.
                If the budget is set to a value known to be insufficient to train a
                Model for the given training set, the training won't be attempted and
                will error.
                The minimum value is 1000 and the maximum is 72000.
            model_display_name (str):
                Optional. If the script produces a managed Vertex AI Model. The display name of
                the Model. The name can be up to 128 characters long and can be consist
                of any UTF-8 characters.

                If not provided upon creation, the job's display_name is used.
            model_labels (Dict[str, str]):
                Optional. The labels with user-defined metadata to
                organize your Models.
                Label keys and values can be no longer than 64
                characters (Unicode codepoints), can only
                contain lowercase letters, numeric characters,
                underscores and dashes. International characters
                are allowed.
                See https://goo.gl/xmQnxf for more information
                and examples of labels.
            model_id (str):
                Optional. The ID to use for the Model produced by this job,
                which will become the final component of the model resource name.
                This value may be up to 63 characters, and valid characters
                are `[a-z0-9_-]`. The first character cannot be a number or hyphen.
            parent_model (str):
                Optional. The resource name or model ID of an existing model.
                The new model uploaded by this job will be a version of `parent_model`.

                Only set this field when training a new version of an existing model.
            is_default_version (bool):
                Optional. When set to True, the newly uploaded model version will
                automatically have alias "default" included. Subsequent uses of
                the model produced by this job without a version specified will
                use this "default" version.

                When set to False, the "default" alias will not be moved.
                Actions targeting the model version produced by this job will need
                to specifically reference this version by ID or alias.

                New model uploads, i.e. version 1, will always be "default" aliased.
            model_version_aliases (Sequence[str]):
                Optional. User provided version aliases so that the model version
                uploaded by this job can be referenced via alias instead of
                auto-generated version ID. A default version alias will be created
                for the first version of the model.

                The format is [a-z][a-zA-Z0-9-]{0,126}[a-z0-9]
            model_version_description (str):
               Optional. The description of the model version being uploaded by this job.
            disable_early_stopping (bool):
                Required. If true, the entire budget is used. This disables the early stopping
                feature. By default, the early stopping feature is enabled, which means
                that training might stop before the entire training budget has been
                used, if further training does no longer brings significant improvement
                to the model.
            export_evaluated_data_items (bool):
                Whether to export the test set predictions to a BigQuery table.
                If False, then the export is not performed.
            export_evaluated_data_items_bigquery_destination_uri (string):
                Optional. URI of desired destination BigQuery table for exported test set predictions.

                Expected format:
                ``bq://<project_id>:<dataset_id>:<table>``

                If not specified, then results are exported to the following auto-created BigQuery
                table:
                ``<project_id>:export_evaluated_examples_<model_name>_<yyyy_MM_dd'T'HH_mm_ss_SSS'Z'>.evaluated_examples``

                Applies only if [export_evaluated_data_items] is True.
            export_evaluated_data_items_override_destination (bool):
                Whether to override the contents of [export_evaluated_data_items_bigquery_destination_uri],
                if the table exists, for exported test set predictions. If False, and the
                table exists, then the training job will fail.

                Applies only if [export_evaluated_data_items] is True and
                [export_evaluated_data_items_bigquery_destination_uri] is specified.
            additional_experiments (List[str]):
                Optional. Additional experiment flags for the automl tables training.
            sync (bool):
                Whether to execute this method synchronously. If False, this method
                will be executed in concurrent Future and any downstream object will
                be immediately returned and synced when the Future has completed.
            create_request_timeout (float):
                Optional. The timeout for the create request in seconds.
        Returns:
            model: The trained Vertex AI Model resource or None if training did not
                produce a Vertex AI Model.

        Raises:
            RuntimeError: If Training job has already been run or is waiting to run.
        """
        if model_display_name:
            utils.validate_display_name(model_display_name)
        if model_labels:
            utils.validate_labels(model_labels)

        if self._is_waiting_to_run():
            raise RuntimeError("AutoML Tabular Training is already scheduled to run.")

        if self._has_run:
            raise RuntimeError("AutoML Tabular Training has already run.")

        if additional_experiments:
            self._add_additional_experiments(additional_experiments)

        return self._run(
            dataset=dataset,
            target_column=target_column,
            training_fraction_split=training_fraction_split,
            validation_fraction_split=validation_fraction_split,
            test_fraction_split=test_fraction_split,
            predefined_split_column_name=predefined_split_column_name,
            timestamp_split_column_name=timestamp_split_column_name,
            weight_column=weight_column,
            budget_milli_node_hours=budget_milli_node_hours,
            model_display_name=model_display_name,
            model_labels=model_labels,
            model_id=model_id,
            model_version_aliases=model_version_aliases,
            model_version_description=model_version_description,
            parent_model=parent_model,
            is_default_version=is_default_version,
            disable_early_stopping=disable_early_stopping,
            export_evaluated_data_items=export_evaluated_data_items,
            export_evaluated_data_items_bigquery_destination_uri=export_evaluated_data_items_bigquery_destination_uri,
            export_evaluated_data_items_override_destination=export_evaluated_data_items_override_destination,
            sync=sync,
            create_request_timeout=create_request_timeout,
        )

    @base.optional_sync()
    def _run(
        self,
        dataset: datasets.TabularDataset,
        target_column: str,
        training_fraction_split: Optional[float] = None,
        validation_fraction_split: Optional[float] = None,
        test_fraction_split: Optional[float] = None,
        predefined_split_column_name: Optional[str] = None,
        timestamp_split_column_name: Optional[str] = None,
        weight_column: Optional[str] = None,
        budget_milli_node_hours: int = 1000,
        model_display_name: Optional[str] = None,
        model_labels: Optional[Dict[str, str]] = None,
        model_id: Optional[str] = None,
        parent_model: Optional[str] = None,
        is_default_version: Optional[bool] = True,
        model_version_aliases: Optional[Sequence[str]] = None,
        model_version_description: Optional[str] = None,
        disable_early_stopping: bool = False,
        export_evaluated_data_items: bool = False,
        export_evaluated_data_items_bigquery_destination_uri: Optional[str] = None,
        export_evaluated_data_items_override_destination: bool = False,
        sync: bool = True,
        create_request_timeout: Optional[float] = None,
    ) -> models.Model:
        """Runs the training job and returns a model.

        If training on a Vertex AI dataset, you can use one of the following split configurations:
            Data fraction splits:
            Any of ``training_fraction_split``, ``validation_fraction_split`` and
            ``test_fraction_split`` may optionally be provided, they must sum to up to 1. If
            the provided ones sum to less than 1, the remainder is assigned to sets as
            decided by Vertex AI. If none of the fractions are set, by default roughly 80%
            of data will be used for training, 10% for validation, and 10% for test.

            Predefined splits:
            Assigns input data to training, validation, and test sets based on the value of a provided key.
            If using predefined splits, ``predefined_split_column_name`` must be provided.
            Supported only for tabular Datasets.

            Timestamp splits:
            Assigns input data to training, validation, and test sets
            based on a provided timestamps. The youngest data pieces are
            assigned to training set, next to validation set, and the oldest
            to the test set.
            Supported only for tabular Datasets.

        Args:
            dataset (datasets.TabularDataset):
                Required. The dataset within the same Project from which data will be used to train the Model. The
                Dataset must use schema compatible with Model being trained,
                and what is compatible should be described in the used
                TrainingPipeline's [training_task_definition]
                [google.cloud.aiplatform.v1beta1.TrainingPipeline.training_task_definition].
                For tabular Datasets, all their data is exported to
                training, to pick and choose from.
            target_column (str):
                Required. The name of the column values of which the Model is to predict.
            training_fraction_split (float):
                Optional. The fraction of the input data that is to be used to train
                the Model. This is ignored if Dataset is not provided.
            validation_fraction_split (float):
                Optional. The fraction of the input data that is to be used to validate
                the Model. This is ignored if Dataset is not provided.
            test_fraction_split (float):
                Optional. The fraction of the input data that is to be used to evaluate
                the Model. This is ignored if Dataset is not provided.
            predefined_split_column_name (str):
                Optional. The key is a name of one of the Dataset's data
                columns. The value of the key (either the label's value or
                value in the column) must be one of {``training``,
                ``validation``, ``test``}, and it defines to which set the
                given piece of data is assigned. If for a piece of data the
                key is not present or has an invalid value, that piece is
                ignored by the pipeline.

                Supported only for tabular and time series Datasets.
            timestamp_split_column_name (str):
                Optional. The key is a name of one of the Dataset's data
                columns. The value of the key values of the key (the values in
                the column) must be in RFC 3339 `date-time` format, where
                `time-offset` = `"Z"` (e.g. 1985-04-12T23:20:50.52Z). If for a
                piece of data the key is not present or has an invalid value,
                that piece is ignored by the pipeline.
                Supported only for tabular and time series Datasets.
                This parameter must be used with training_fraction_split,
                validation_fraction_split, and test_fraction_split.
            weight_column (str):
                Optional. Name of the column that should be used as the weight column.
                Higher values in this column give more importance to the row
                during Model training. The column must have numeric values between 0 and
                10000 inclusively, and 0 value means that the row is ignored.
                If the weight column field is not set, then all rows are assumed to have
                equal weight of 1.
            budget_milli_node_hours (int):
                Optional. The train budget of creating this Model, expressed in milli node
                hours i.e. 1,000 value in this field means 1 node hour.
                The training cost of the model will not exceed this budget. The final
                cost will be attempted to be close to the budget, though may end up
                being (even) noticeably smaller - at the backend's discretion. This
                especially may happen when further model training ceases to provide
                any improvements.
                If the budget is set to a value known to be insufficient to train a
                Model for the given training set, the training won't be attempted and
                will error.
                The minimum value is 1000 and the maximum is 72000.
            model_display_name (str):
                Optional. If the script produces a managed Vertex AI Model. The display name of
                the Model. The name can be up to 128 characters long and can be consist
                of any UTF-8 characters.

                If not provided upon creation, the job's display_name is used.
            model_labels (Dict[str, str]):
                Optional. The labels with user-defined metadata to
                organize your Models.
                Label keys and values can be no longer than 64
                characters (Unicode codepoints), can only
                contain lowercase letters, numeric characters,
                underscores and dashes. International characters
                are allowed.
                See https://goo.gl/xmQnxf for more information
                and examples of labels.
            model_id (str):
                Optional. The ID to use for the Model produced by this job,
                which will become the final component of the model resource name.
                This value may be up to 63 characters, and valid characters
                are `[a-z0-9_-]`. The first character cannot be a number or hyphen.
            parent_model (str):
                Optional. The resource name or model ID of an existing model.
                The new model uploaded by this job will be a version of `parent_model`.

                Only set this field when training a new version of an existing model.
            is_default_version (bool):
                Optional. When set to True, the newly uploaded model version will
                automatically have alias "default" included. Subsequent uses of
                the model produced by this job without a version specified will
                use this "default" version.

                When set to False, the "default" alias will not be moved.
                Actions targeting the model version produced by this job will need
                to specifically reference this version by ID or alias.

                New model uploads, i.e. version 1, will always be "default" aliased.
            model_version_aliases (Sequence[str]):
                Optional. User provided version aliases so that the model version
                uploaded by this job can be referenced via alias instead of
                auto-generated version ID. A default version alias will be created
                for the first version of the model.

                The format is [a-z][a-zA-Z0-9-]{0,126}[a-z0-9]
            model_version_description (str):
               Optional. The description of the model version being uploaded by this job.
            disable_early_stopping (bool):
                Required. If true, the entire budget is used. This disables the early stopping
                feature. By default, the early stopping feature is enabled, which means
                that training might stop before the entire training budget has been
                used, if further training does no longer brings significant improvement
                to the model.
            export_evaluated_data_items (bool):
                Whether to export the test set predictions to a BigQuery table.
                If False, then the export is not performed.
            export_evaluated_data_items_bigquery_destination_uri (string):
                Optional. URI of desired destination BigQuery table for exported test set predictions.

                Expected format:
                ``bq://<project_id>:<dataset_id>:<table>``

                If not specified, then results are exported to the following auto-created BigQuery
                table:
                ``<project_id>:export_evaluated_examples_<model_name>_<yyyy_MM_dd'T'HH_mm_ss_SSS'Z'>.evaluated_examples``

                Applies only if [export_evaluated_data_items] is True.
            export_evaluated_data_items_override_destination (bool):
                Whether to override the contents of [export_evaluated_data_items_bigquery_destination_uri],
                if the table exists, for exported test set predictions. If False, and the
                table exists, then the training job will fail.

                Applies only if [export_evaluated_data_items] is True and
                [export_evaluated_data_items_bigquery_destination_uri] is specified.
            sync (bool):
                Whether to execute this method synchronously. If False, this method
                will be executed in concurrent Future and any downstream object will
                be immediately returned and synced when the Future has completed.
            create_request_timeout (float):
                Optional. The timeout for the create request in seconds.

        Returns:
            model: The trained Vertex AI Model resource or None if training did not
                produce a Vertex AI Model.
        """

        training_task_definition = schema.training_job.definition.automl_tabular

        # auto-populate transformations
        if self._column_transformations is None:
            _LOGGER.info(
                "No column transformations provided, so now retrieving columns from dataset in order to set default column transformations."
            )

            (
                self._column_transformations,
                column_names,
            ) = column_transformations_utils.get_default_column_transformations(
                dataset=dataset, target_column=target_column
            )

            _LOGGER.info(
                "The column transformation of type 'auto' was set for the following columns: %s."
                % column_names
            )

        training_task_inputs_dict = {
            # required inputs
            "targetColumn": target_column,
            "transformations": self._column_transformations,
            "trainBudgetMilliNodeHours": budget_milli_node_hours,
            # optional inputs
            "weightColumnName": weight_column,
            "disableEarlyStopping": disable_early_stopping,
            "optimizationObjective": self._optimization_objective,
            "predictionType": self._optimization_prediction_type,
            "optimizationObjectiveRecallValue": self._optimization_objective_recall_value,
            "optimizationObjectivePrecisionValue": self._optimization_objective_precision_value,
        }

        final_export_eval_bq_uri = export_evaluated_data_items_bigquery_destination_uri
        if final_export_eval_bq_uri and not final_export_eval_bq_uri.startswith(
            "bq://"
        ):
            final_export_eval_bq_uri = f"bq://{final_export_eval_bq_uri}"

        if export_evaluated_data_items:
            training_task_inputs_dict["exportEvaluatedDataItemsConfig"] = {
                "destinationBigqueryUri": final_export_eval_bq_uri,
                "overrideExistingTable": export_evaluated_data_items_override_destination,
            }

        if self._additional_experiments:
            training_task_inputs_dict[
                "additionalExperiments"
            ] = self._additional_experiments

        model = gca_model.Model(
            display_name=model_display_name or self._display_name,
            labels=model_labels or self._labels,
            encryption_spec=self._model_encryption_spec,
        )

        return self._run_job(
            training_task_definition=training_task_definition,
            training_task_inputs=training_task_inputs_dict,
            dataset=dataset,
            training_fraction_split=training_fraction_split,
            validation_fraction_split=validation_fraction_split,
            test_fraction_split=test_fraction_split,
            predefined_split_column_name=predefined_split_column_name,
            timestamp_split_column_name=timestamp_split_column_name,
            model=model,
            model_id=model_id,
            parent_model=parent_model,
            is_default_version=is_default_version,
            model_version_aliases=model_version_aliases,
            model_version_description=model_version_description,
            create_request_timeout=create_request_timeout,
        )

    @property
    def _model_upload_fail_string(self) -> str:
        """Helper property for model upload failure."""
        return (
            f"Training Pipeline {self.resource_name} is not configured to upload a "
            "Model."
        )

    def _add_additional_experiments(self, additional_experiments: List[str]):
        """Add experiment flags to the training job.
        Args:
            additional_experiments (List[str]):
                Experiment flags that can enable some experimental training features.
        """
        self._additional_experiments.extend(additional_experiments)

    @staticmethod
    def get_auto_column_specs(
        dataset: datasets.TabularDataset,
        target_column: str,
    ) -> Dict[str, str]:
        """Returns a dict with all non-target columns as keys and 'auto' as values.

        Example usage:

        column_specs = training_jobs.AutoMLTabularTrainingJob.get_auto_column_specs(
            dataset=my_dataset,
            target_column="my_target_column",
        )

        Args:
            dataset (datasets.TabularDataset):
                Required. Intended dataset.
            target_column(str):
                Required. Intended target column.
        Returns:
            Dict[str, str]
                Column names as keys and 'auto' as values
        """
        column_names = [
            column for column in dataset.column_names if column != target_column
        ]
        column_specs = {column: "auto" for column in column_names}
        return column_specs

    class column_data_types:
        AUTO = "auto"
        NUMERIC = "numeric"
        CATEGORICAL = "categorical"
        TIMESTAMP = "timestamp"
        TEXT = "text"
        REPEATED_NUMERIC = "repeated_numeric"
        REPEATED_CATEGORICAL = "repeated_categorical"
        REPEATED_TEXT = "repeated_text"


class AutoMLForecastingTrainingJob(_ForecastingTrainingJob):
    """Class to train AutoML forecasting models.

    The `AutoMLForecastingTrainingJob` class uses the AutoML training method
    to train and run a forecasting model. The `AutoML` training method is a good
    choice for most forecasting use cases. If your use case doesn't benefit from
    the `Seq2seq` or the `Temporal fusion transformer` training method offered
    by the
    [`SequenceToSequencePlusForecastingTrainingJob`](https://cloud.google.com/python/docs/reference/aiplatform/latest/google.cloud.aiplatform.SequenceToSequencePlusForecastingTrainingJob)
    and
    [`TemporalFusionTransformerForecastingTrainingJob`]https://cloud.google.com/python/docs/reference/aiplatform/latest/google.cloud.aiplatform.TemporalFusionTransformerForecastingTrainingJob)
    classes respectively, then `AutoML` is likely the best training method for
    your forecasting predictions.

    For sample code that shows you how to use `AutoMLForecastingTrainingJob` see
    the [Create a training pipeline forecasting sample](https://github.com/googleapis/python-aiplatform/blob/8ddc062669044ac0889d9f27c93a8b36c1140433/samples/model-builder/create_training_pipeline_forecasting_sample.py)
    on GitHub.
    """

    _model_type = "AutoML"
    _training_task_definition = schema.training_job.definition.automl_forecasting
    _supported_training_schemas = (schema.training_job.definition.automl_forecasting,)


class SequenceToSequencePlusForecastingTrainingJob(_ForecastingTrainingJob):
    """Class to train Sequence to Sequence (Seq2Seq) forecasting models.

    The `SequenceToSequencePlusForecastingTrainingJob` class uses the `Seq2seq+`
    training method to train and run a forecasting model. The `Seq2seq+`
    training method is a good choice for experimentation. Its algorithm is
    simpler and uses a smaller search space than the `AutoML` option. `Seq2seq+`
    is a good option if you want fast results and your datasets are smaller than
    1 GB.

    For sample code that shows you how to use
    `SequenceToSequencePlusForecastingTrainingJob`, see the [Create a training
    pipeline forecasting Seq2seq
    sample](https://github.com/googleapis/python-aiplatform/blob/8ddc062669044ac0889d9f27c93a8b36c1140433/samples/model-builder/create_training_pipeline_forecasting_seq2seq_sample.py)
    on GitHub.
    """

    _model_type = "Seq2Seq"
    _training_task_definition = schema.training_job.definition.seq2seq_plus_forecasting
    _supported_training_schemas = (
        schema.training_job.definition.seq2seq_plus_forecasting,
    )


class TemporalFusionTransformerForecastingTrainingJob(_ForecastingTrainingJob):
    """Class to train Temporal Fusion Transformer (TFT) forecasting models.

    The `TemporalFusionTransformerForecastingTrainingJob` class uses the
    Temporal Fusion Transformer (TFT) training method to train and run a
    forecasting model. The TFT training method implements an attention-based
    deep neural network (DNN) model that uses a multi-horizon forecasting task
    to produce predictions.

    For sample code that shows you how to use
    `TemporalFusionTransformerForecastingTrainingJob, see the
    [Create a training pipeline forecasting temporal fusion transformer
    sample](https://github.com/googleapis/python-aiplatform/blob/8ddc062669044ac0889d9f27c93a8b36c1140433/samples/model-builder/create_training_pipeline_forecasting_tft_sample.py)
    on GitHub.
    """

    _model_type = "TFT"
    _training_task_definition = schema.training_job.definition.tft_forecasting
    _supported_training_schemas = (schema.training_job.definition.tft_forecasting,)


class TimeSeriesDenseEncoderForecastingTrainingJob(_ForecastingTrainingJob):
    """Class to train Time series Dense Encoder (TiDE) forecasting models.

    The `TimeSeriesDenseEncoderForecastingTrainingJob` class uses the
    Time-series Dense Encoder (TiDE) training method to train and run a
    forecasting model. TiDE uses a
    [multi-layer perceptron](https://arxiv.org/abs/2304.08424) (MLP) to provide
    the speed of forecasting linear models with covariates and non-linear
    dependencies. For more information about TiDE, see
    [Recent advances in deep long-horizon forecasting](https://blog.research.google/2023/04/recent-advances-in-deep-long-horizon.html)
    and this
    [TiDE blog post](https://cloud.google.com/blog/products/ai-machine-learning/vertex-ai-forecasting).
    """

    _model_type = "TiDE"
    _training_task_definition = schema.training_job.definition.tide_forecasting
    _supported_training_schemas = (schema.training_job.definition.tide_forecasting,)


class AutoMLImageTrainingJob(_TrainingJob):
    _supported_training_schemas = (
        schema.training_job.definition.automl_image_classification,
        schema.training_job.definition.automl_image_object_detection,
    )

    def __init__(
        self,
        display_name: Optional[str] = None,
        prediction_type: str = "classification",
        multi_label: bool = False,
        model_type: str = "CLOUD",
        base_model: Optional[models.Model] = None,
        incremental_train_base_model: Optional[models.Model] = None,
        project: Optional[str] = None,
        location: Optional[str] = None,
        credentials: Optional[auth_credentials.Credentials] = None,
        labels: Optional[Dict[str, str]] = None,
        training_encryption_spec_key_name: Optional[str] = None,
        model_encryption_spec_key_name: Optional[str] = None,
        checkpoint_name: Optional[str] = None,
        trainer_config: Optional[Dict[str, str]] = None,
        metric_spec: Optional[Dict[str, str]] = None,
        parameter_spec: Optional[
            Dict[
                str,
                Union[
                    hpt.DoubleParameterSpec,
                    hpt.IntegerParameterSpec,
                    hpt.CategoricalParameterSpec,
                    hpt.DiscreteParameterSpec,
                ],
            ]
        ] = None,
        search_algorithm: Optional[str] = None,
        measurement_selection: Optional[str] = None,
    ):
        """Creates an AutoML image training job.

        Use the `AutoMLImageTrainingJob` class to create, train, and return an
        image model. For more information about working with image data models
        in Vertex AI, see [Image data](https://cloud.google.com/vertex-ai/docs/training-overview#image_data).

        For an example of how to use the `AutoMLImageTrainingJob` class, see the
        tutorial in the [AutoML image
        classification](https://github.com/GoogleCloudPlatform/vertex-ai-samples/blob/main/notebooks/official/migration/sdk-automl-text-classification-batch-prediction.ipynb)
        notebook on GitHub.

        Args:
            display_name (str): Optional. The user-defined name of the training
                pipeline. The name must contain 128 or fewer UTF-8 characters.
            prediction_type (str): The type of prediction the model produces.
                Valid values are: `classification` to predict one of multiple
                target values that are selected from each row. `object_detection`
                to predict a value based on its relation to other values.
                `object_detection` is available only for columns that contain
                semantically numeric values. For example, columns might contain
                numeric values stored as an integer, float, or string.
            multi_label (bool = False): Required. If `false`, a single-label
                (multi-class) model is
                trained (i.e. assuming that for each image just up to one
                annotation may be applicable). If `true`, a multi-label model is
                trained. If you pass in `true`, it's assumed multiple
                annotations might apply to each image. Multi-label models are
                supported only when `prediction_type` is set to
                `classification`. If `multi_label` is `true` and
                `prediction_type` is `object_detection`, then then `multi_label`
                is ignored. The default value is `false`.
            model_type (str = "CLOUD"): Required.
                The type of model to create. The following are the valid
                values:

                `CLOUD` - The default for image classification. This model type
                is deigned to work in Google Cloud and can't be exported.

                `CLOUD_1` - This model type is deigned to work in Google Cloud
                and can't be exported. This model type is expected to have
                higher prediction accuracy than `CLOUD`.

                `CLOUD_HIGH_ACCURACY_1` - The default for image object detection.
                This model type is deigned to work in Google Cloud and can't be
                exported. It's designed to have a higher latency and a higher
                prediction quality than other cloud model types.

                `CLOUD_LOW_LATENCY_1` - This model type is deigned to work in
                Google Cloud and can't be exported. It's also designed to have
                low latency, but might might lower prediction quality than other
                cloud model types.

                `MOBILE_TF_LOW_LATENCY_1` - This model type is deigned to work
                in Google Cloud and can be exported as a TensorFlow or Core ML
                model for use on a mobile or edge device. It's also designed to
                have to have low latency, but might have lower prediction
                quality than other mobile models types.

                `MOBILE_TF_VERSATILE_1` - This model type is deigned to work in
                Google Cloud and can be exported as a TensorFlow or Core ML
                model for use on a mobile or edge device.

                `MOBILE_TF_HIGH_ACCURACY_1` - This model type is deigned to work
                in Google Cloud and can be exported as a TensorFlow or Core ML
                model for use on a mobile or edge device. It's designed to have
                a higher latency, but should also have a higher prediction
                quality than other mobile model types.

                `EFFICIENTNET` - This model type is available in the Vertex
                Model Garden image classification training with customizable
                hyperparameters. It's deigned to work in Google Cloud and can't
                be exported.

                `VIT` - This model type is available in the Vertex Model
                Garden image classification training with customizable
                hyperparameters. Best tailored to be used
                within Google Cloud, and cannot be exported externally.

                `MAXVIT` - This model type is available in the Vertex Model
                Garden image classification training with customizable
                hyperparameters. It's deigned to work in Google Cloud and can't
                be exported.

                `COCA` - This model type is available in the Vertex Model Garden
                image classification training with customizable hyperparameters.
                It's deigned to work in Google Cloud and can't be exported.

                `SPINENET` - This model type is available in the Vertex Model
                Garden image object detection training with customizable
                hyperparameters. It's deigned to work in Google Cloud and can't
                be exported.

                `YOLO` - This model type is available in the Vertex Model Garden
                image object detection training with customizable
                hyperparameters. It's deigned to work in Google Cloud and can't
                be exported.
            base_model: Optional[models.Model] = Optional. You can specify a
                `base_model` for image classification models only. If it is
                specified, the new model is trained based on the `base_model`.
                Otherwise, the new model is trained from scratch. The base model
                must be in the same Google Project and region as the new model to
                train, and it must have the same `model_type`.
            incremental_train_base_model: Optional[models.Model] = Optional. You
                can specify an `incremental_train_base_model` for image
                classification models and object detection models only. If
                specified, the new model is incrementally trained using an
                existing model as the starting point. This can reduce the
                training time. If not specified, the new model is trained from
                scratch. The model specified in `incremental_train_base_model`
                model must be in the same Google Project and region as the new
                model to train, and it must have the same `prediction_type` and
                `model_type`.
            project (str): Optional. The Google Cloud region where this where
                the training runs. This region overrides the region that was set
                by `aiplatform.init`.
            location (str): Optional. Location to run training in. Overrides
                location set in aiplatform.init.
            credentials (auth_credentials.Credentials): Optional. The
                credentials that are used to train the model. These credentials
                override the credentials set by `aiplatform.init`.
            labels (Dict[str, str]): Optional. Labels with user-defined metadata
                to organize your training pipelines. The maximum length of a key
                and value is 64 unicode characters. Labels and keys can contain
                only lowercase letters, numeric characters, underscores, and
                dashes. International characters are allowed. For more
                information and examples of using labels, see [Using labels to
                organize Google Cloud Platform
                resources](https://goo.gl/xmQnxf).
            training_encryption_spec_key_name (Optional[str]): Optional. The Cloud
                KMS resource identifier of the customer managed encryption key
                used to protect the training pipeline. The key has the following
                format:
                `projects/my-project/locations/my-region/keyRings/my-kr/cryptoKeys/my-key`.
                The key needs to be in the same region as where the compute
                resource is created. If set, the key secures the training
                pipeline and overrides the key set using `aiplatform.init`. The
                model trained by this training pipeline is also secured by this
                key if `model_encryption_spec_key_name` is not set separately.
            model_encryption_spec_key_name (Optional[str]): Optional. The Cloud
                KMS resource identifier of the customer managed encryption key
                used to protect the model. The key has the following format:
                `projects/my-project/locations/my-region/keyRings/my-kr/cryptoKeys/my-key`.
                The key needs to be in the same region as where the compute
                resource is created. If set, the key secures the trained model
                and overrides the key set using `aiplatform.init`.
            checkpoint_name: Optional[str] = Optional. The field is reserved for
                Model Garden model training and is based on the provided
                pre-trained model checkpoint. `checkpoint_name` is needed only
                when `model_type` is set to `EFFICIENTNET`, `VIT`, `COCA`,
                `SPINENET`, or `YOLO`.
            trainer_config: Optional[Dict[str, str]] = None, Optional. A field
                that's used with the Model Garden model training when passing
                customized configs for the trainer. The following is an example
                that uses all `trainer_config` parameters:

                ```py
                trainer_config = {
                    'global_batch_size': '8',
                    'learning_rate': '0.001',
                    'optimizer_type': 'sgd',
                    'optimizer_momentum': '0.9',
                    'train_steps': '10000',
                    'accelerator_count': '1',
                    'anchor_size': '8',  -- IOD only
                }
                ```

                `trainer_config` is required for only Model Garden models when
                `model_type` is `EFFICIENTNET`, `VIT`, `COCA`, `SPINENET`, or
                `YOLO`.
            metric_spec: Dict[str, str] Required. A dictionary that represents
                metrics use for optimization. The dictionary key is the
                `metric_id` that's reported by your training job and can be
                'loss' or 'accuracy'. The dictionary value is the optimization
                goal of the metric, and can be 'minimize' or 'maximize'. The
                following is an example of a `metric_spec`: `metric_spec =
                {'loss': 'minimize', 'accuracy': 'maximize'}`. `metric_spec` is
                required for only Model Garden models when `model_type` is
                `EFFICIENTNET`, `VIT`, `COCA`, `SPINENET`, or `YOLO`.
            parameter_spec (Dict[str, hpt._ParameterSpec]): Required. A
                dictionary representing parameters to use for optimization.
                The dictionary key is the `metric_id` that's reported by your
                training job as a command line keyword argument. The dictionary
                value is the parameter specification of the metric. The
                following is an example of how to specify a `parameter_spec`:

                ```py
                from google.cloud.aiplatform import hpt as hpt

                parameter_spec = {
                    'learning_rate': hpt.DoubleParameterSpec(min=1e-7, max=1, scale='linear'),
                }
                ```

                Supported parameter specifications can be found in
                `aiplatform.hyperparameter_tuning`. The following parameter
                specifications are supported: `Union[DoubleParameterSpec,
                IntegerParameterSpec, CategoricalParameterSpec,
                DiscreteParameterSpec]`. `parameter_spec `is required for only
                Model Garden models when `model_type` is `EFFICIENTNET`, `VIT`,
                `COCA`, `SPINENET`, or `YOLO`.
            search_algorithm (str): The search algorithm that's specified for
                the study. The search algorithm can be one of the following:

                `None` - If you don't specify an algorithm, your job uses the
                default Vertex AI algorithm. The default algorithm applies
                Bayesian optimization to arrive at the optimal solution with a
                more effective search over the parameter space.

                'grid' - A simple grid search within the feasible space. This
                option is particularly useful if you want to specify a quantity
                of trials that is greater than the number of points in the
                feasible space. In these cases, if you don't specify a grid
                search, the Vertex AI default algorithm might generate duplicate
                suggestions. To use grid search, all parameter specifications
                must be `IntegerParameterSpec`, `CategoricalParameterSpec`, or
                `DiscreteParameterSpec`.

                'random' - A simple random search within the feasible space.

                `search_algorithm` is required for only Model Garden models when
                `model_type` is `EFFICIENTNE`T`, `VIT`, `COCA`, `SPINENET`, or
                `YOLO`.
            measurement_selection (str): Indicates which measurement to use
                when the service selects the final measurement from previously
                reported intermediate measurements. `measurement_selection` can
                be `best` or `last`. If you expect your measurements to
                monotonically improve, then choose `last`. If your system can
                over-train and you expect the performance to improve and then
                start to decline, then choose `best`. If your measurements are
                significantly noisy or not reproducible, then `best` is likely
                to be over-optimistic, so in this case `last` is the preferred
                option. `measurement_selection` is required for only Model
                Garden models when `model_type` is `EFFICIENTNET`, `VIT`,
                `COCA`, `SPINENET`, or `YOLO`.

        Raises:
            ValueError: When an invalid `prediction_type` or `model_type` is
                provided.
        """
        if not display_name:
            display_name = self.__class__._generate_display_name()

        valid_model_types = constants.AUTOML_IMAGE_PREDICTION_MODEL_TYPES.get(
            prediction_type, None
        )

        if not valid_model_types:
            raise ValueError(
                f"'{prediction_type}' is not a supported prediction type for AutoML"
                " Image Training. Please choose one of:"
                f" {tuple(constants.AUTOML_IMAGE_PREDICTION_MODEL_TYPES.keys())}."
            )

        # Override default model_type for object_detection
        if model_type == "CLOUD" and prediction_type == "object_detection":
            model_type = "CLOUD_HIGH_ACCURACY_1"

        if model_type not in valid_model_types:
            raise ValueError(
                f"'{model_type}' is not a supported model_type for prediction_type of"
                f" '{prediction_type}'. Please choose one of:"
                f" {tuple(valid_model_types)}"
            )

        if base_model and prediction_type != "classification":
            raise ValueError(
                "Training with a `base_model` is only supported in AutoML Image"
                f" Classification. However '{prediction_type}' was provided as"
                " `prediction_type`."
            )

        model_garden_models = constants.MODEL_GARDEN_ICN_MODEL_TYPES.union(
            constants.MODEL_GARDEN_IOD_MODEL_TYPES
        )

        if checkpoint_name and model_type not in model_garden_models:
            raise ValueError(
                "Training with a `checkpoint_name` is only supported in Model Garden"
                f" models {tuple(model_garden_models)}. However,"
                f" '{model_type}' was provided as `model_type`."
            )

        if trainer_config and model_type not in model_garden_models:
            raise ValueError(
                "Training with a `trainer_config` is only supported in Model Garden"
                f" models {tuple(model_garden_models)}. However,"
                f" '{model_type}' was provided as `model_type`."
            )

        if metric_spec and model_type not in model_garden_models:
            raise ValueError(
                "Training with a `metric_spec` is only supported in Model Garden"
                f" models {tuple(model_garden_models)}. However,"
                f" '{model_type}' was provided as `model_type`."
            )

        if parameter_spec and model_type not in model_garden_models:
            raise ValueError(
                "Training with a `parameter_spec` is only supported in Model Garden"
                f" models {tuple(model_garden_models)}. However,"
                f" '{model_type}' was provided as `model_type`."
            )

        if search_algorithm and model_type not in model_garden_models:
            raise ValueError(
                "Training with a `search_algorithm` is only supported in Model Garden"
                f" models {tuple(model_garden_models)}. However,"
                f" '{model_type}' was provided as `model_type`."
            )

        if measurement_selection and model_type not in model_garden_models:
            raise ValueError(
                "Training with a `measurement_selection` is only supported in Model Garden"
                f" models {tuple(model_garden_models)}. However,"
                f" '{model_type}' was provided as `model_type`."
            )

        metrics = (
            [
                gca_study_compat.StudySpec.MetricSpec(
                    metric_id=metric_id, goal=goal.upper()
                )
                for metric_id, goal in metric_spec.items()
            ]
            if metric_spec
            else []
        )

        parameters = (
            [
                parameter._to_parameter_spec(parameter_id=parameter_id)
                for parameter_id, parameter in parameter_spec.items()
            ]
            if parameter_spec
            else []
        )

        study_spec = gca_study_compat.StudySpec(
            metrics=metrics,
            parameters=parameters,
            algorithm=hpt.SEARCH_ALGORITHM_TO_PROTO_VALUE[search_algorithm],
            measurement_selection_type=hpt.MEASUREMENT_SELECTION_TO_PROTO_VALUE[
                measurement_selection
            ],
        )

        super().__init__(
            display_name=display_name,
            project=project,
            location=location,
            credentials=credentials,
            labels=labels,
            training_encryption_spec_key_name=training_encryption_spec_key_name,
            model_encryption_spec_key_name=model_encryption_spec_key_name,
        )

        self._model_type = model_type
        self._prediction_type = prediction_type
        self._multi_label = multi_label
        self._base_model = base_model
        self._incremental_train_base_model = incremental_train_base_model
        self._checkpoint_name = checkpoint_name
        self._trainer_config = trainer_config
        self._study_spec = study_spec

    def run(
        self,
        dataset: datasets.ImageDataset,
        training_fraction_split: Optional[float] = None,
        validation_fraction_split: Optional[float] = None,
        test_fraction_split: Optional[float] = None,
        training_filter_split: Optional[str] = None,
        validation_filter_split: Optional[str] = None,
        test_filter_split: Optional[str] = None,
        budget_milli_node_hours: Optional[int] = None,
        model_display_name: Optional[str] = None,
        model_labels: Optional[Dict[str, str]] = None,
        model_id: Optional[str] = None,
        parent_model: Optional[str] = None,
        is_default_version: Optional[bool] = True,
        model_version_aliases: Optional[Sequence[str]] = None,
        model_version_description: Optional[str] = None,
        disable_early_stopping: bool = False,
        sync: bool = True,
        create_request_timeout: Optional[float] = None,
    ) -> models.Model:
        """Runs the AutoML Image training job and returns a model.

        If training on a Vertex AI dataset, you can use one of the following split
        configurations:
            Data fraction splits:
            Any of ``training_fraction_split``, ``validation_fraction_split`` and
            ``test_fraction_split`` may optionally be provided, they must sum to up
            to 1. If
            the provided ones sum to less than 1, the remainder is assigned to sets
            as
            decided by Vertex AI. If none of the fractions are set, by default
            roughly 80%
            of data will be used for training, 10% for validation, and 10% for test.

            Data filter splits:
            Assigns input data to training, validation, and test sets
            based on the given filters, data pieces not matched by any
            filter are ignored. Currently only supported for Datasets
            containing DataItems.
            If any of the filters in this message are to match nothing, then
            they can be set as '-' (the minus sign).
            If using filter splits, all of ``training_filter_split``,
            ``validation_filter_split`` and
            ``test_filter_split`` must be provided.
            Supported only for unstructured Datasets.

        Args:
            dataset (datasets.ImageDataset): Required. The dataset within the same
              Project from which data will be used to train the Model. The Dataset
              must use schema compatible with Model being trained, and what is
              compatible should be described in the used TrainingPipeline's
              [training_task_definition]
              [google.cloud.aiplatform.v1beta1.TrainingPipeline.training_task_definition].
              For tabular Datasets, all their data is exported to training, to pick
              and choose from.
            training_fraction_split (float): Optional. The fraction of the input
              data that is to be used to train the Model. This is ignored if Dataset
              is not provided.
            validation_fraction_split (float): Optional. The fraction of the input
              data that is to be used to validate the Model. This is ignored if
              Dataset is not provided.
            test_fraction_split (float): Optional. The fraction of the input data
              that is to be used to evaluate the Model. This is ignored if Dataset
              is not provided.
            training_filter_split (str): Optional. A filter on DataItems of the
              Dataset. DataItems that match this filter are used to train the Model.
              A filter with same syntax as the one used in
              DatasetService.ListDataItems may be used. If a single DataItem is
              matched by more than one of the FilterSplit filters, then it is
              assigned to the first set that applies to it in the training,
              validation, test order. This is ignored if Dataset is not provided.
            validation_filter_split (str): Optional. A filter on DataItems of the
              Dataset. DataItems that match this filter are used to validate the
              Model. A filter with same syntax as the one used in
              DatasetService.ListDataItems may be used. If a single DataItem is
              matched by more than one of the FilterSplit filters, then it is
              assigned to the first set that applies to it in the training,
              validation, test order. This is ignored if Dataset is not provided.
            test_filter_split (str): Optional. A filter on DataItems of the Dataset.
              DataItems that match this filter are used to test the Model. A filter
              with same syntax as the one used in DatasetService.ListDataItems may
              be used. If a single DataItem is matched by more than one of the
              FilterSplit filters, then it is assigned to the first set that applies
              to it in the training, validation, test order. This is ignored if
              Dataset is not provided.
            budget_milli_node_hours (int): Optional. The train budget of creating
              this Model, expressed in milli node hours i.e. 1,000 value in this
              field means 1 node hour.  Defaults by `prediction_type`:
              `classification` - For Cloud models the budget must be: 8,000 -
              800,000 milli node hours (inclusive). The default value is 192,000
              which represents one day in wall time, assuming 8 nodes are used.
              `object_detection` - For Cloud models the budget must be: 20,000 -
              900,000 milli node hours (inclusive). The default value is 216,000
              which represents one day in wall time, assuming 9 nodes are used.  The
              training cost of the model will not exceed this budget. The final cost
              will be attempted to be close to the budget, though may end up being
              (even) noticeably smaller - at the backend's discretion. This
              especially may happen when further model training ceases to provide
              any improvements. If the budget is set to a value known to be
              insufficient to train a Model for the given training set, the training
              won't be attempted and will error.
            model_display_name (str): Optional. The display name of the managed
              Vertex AI Model. The name can be up to 128 characters long and can be
              consist of any UTF-8 characters. If not provided upon creation, the
              job's display_name is used.
            model_labels (Dict[str, str]): Optional. The labels with user-defined
              metadata to organize your Models. Label keys and values can be no
              longer than 64 characters (Unicode codepoints), can only contain
              lowercase letters, numeric characters, underscores and dashes.
              International characters are allowed. See https://goo.gl/xmQnxf for
              more information and examples of labels.
            model_id (str): Optional. The ID to use for the Model produced by this
              job, which will become the final component of the model resource name.
              This value may be up to 63 characters, and valid characters are
              `[a-z0-9_-]`. The first character cannot be a number or hyphen.
            parent_model (str): Optional. The resource name or model ID of an
              existing model. The new model uploaded by this job will be a version
              of `parent_model`.  Only set this field when training a new version of
              an existing model.
            is_default_version (bool): Optional. When set to True, the newly
              uploaded model version will automatically have alias "default"
              included. Subsequent uses of the model produced by this job without a
              version specified will use this "default" version.  When set to False,
              the "default" alias will not be moved. Actions targeting the model
              version produced by this job will need to specifically reference this
              version by ID or alias.  New model uploads, i.e. version 1, will
              always be "default" aliased.
            model_version_aliases (Sequence[str]): Optional. User provided version
              aliases so that the model version uploaded by this job can be
              referenced via alias instead of auto-generated version ID. A default
              version alias will be created for the first version of the model.  The
              format is [a-z][a-zA-Z0-9-]{0,126}[a-z0-9]
            model_version_description (str): Optional. The description of the model
              version being uploaded by this job.
            disable_early_stopping: bool = False Required. If true, the entire
              budget is used. This disables the early stopping feature. By default,
              the early stopping feature is enabled, which means that training might
              stop before the entire training budget has been used, if further
              training does no longer brings significant improvement to the model.
            sync: bool = True Whether to execute this method synchronously. If
              False, this method will be executed in concurrent Future and any
              downstream object will be immediately returned and synced when the
              Future has completed.
            create_request_timeout (float): Optional. The timeout for the create
              request in seconds.

        Returns:
            model: The trained Vertex AI Model resource or None if training did not
                produce a Vertex AI Model.

        Raises:
            RuntimeError: If Training job has already been run or is waiting to run.
        """

        if model_display_name:
            utils.validate_display_name(model_display_name)
        if model_labels:
            utils.validate_labels(model_labels)

        if self._is_waiting_to_run():
            raise RuntimeError("AutoML Image Training is already scheduled to run.")

        if self._has_run:
            raise RuntimeError("AutoML Image Training has already run.")

        return self._run(
            dataset=dataset,
            base_model=self._base_model,
            incremental_train_base_model=self._incremental_train_base_model,
            training_fraction_split=training_fraction_split,
            validation_fraction_split=validation_fraction_split,
            test_fraction_split=test_fraction_split,
            training_filter_split=training_filter_split,
            validation_filter_split=validation_filter_split,
            test_filter_split=test_filter_split,
            budget_milli_node_hours=budget_milli_node_hours,
            model_display_name=model_display_name,
            model_labels=model_labels,
            model_id=model_id,
            parent_model=parent_model,
            is_default_version=is_default_version,
            model_version_aliases=model_version_aliases,
            model_version_description=model_version_description,
            disable_early_stopping=disable_early_stopping,
            sync=sync,
            create_request_timeout=create_request_timeout,
        )

    @base.optional_sync()
    def _run(
        self,
        dataset: datasets.ImageDataset,
        base_model: Optional[models.Model] = None,
        incremental_train_base_model: Optional[models.Model] = None,
        training_fraction_split: Optional[float] = None,
        validation_fraction_split: Optional[float] = None,
        test_fraction_split: Optional[float] = None,
        training_filter_split: Optional[str] = None,
        validation_filter_split: Optional[str] = None,
        test_filter_split: Optional[str] = None,
        budget_milli_node_hours: int = 1000,
        model_display_name: Optional[str] = None,
        model_labels: Optional[Dict[str, str]] = None,
        model_id: Optional[str] = None,
        parent_model: Optional[str] = None,
        is_default_version: Optional[bool] = True,
        model_version_aliases: Optional[Sequence[str]] = None,
        model_version_description: Optional[str] = None,
        disable_early_stopping: bool = False,
        sync: bool = True,
        create_request_timeout: Optional[float] = None,
    ) -> models.Model:
        """Runs the training job and returns a model.

        If training on a Vertex AI dataset, you can use one of the following split
        configurations:
            Data fraction splits:
            Any of ``training_fraction_split``, ``validation_fraction_split`` and
            ``test_fraction_split`` may optionally be provided, they must sum to up
            to 1. If
            the provided ones sum to less than 1, the remainder is assigned to sets
            as
            decided by Vertex AI. If none of the fractions are set, by default
            roughly 80%
            of data will be used for training, 10% for validation, and 10% for test.

            Data filter splits:
            Assigns input data to training, validation, and test sets
            based on the given filters, data pieces not matched by any
            filter are ignored. Currently only supported for Datasets
            containing DataItems.
            If any of the filters in this message are to match nothing, then
            they can be set as '-' (the minus sign).
            If using filter splits, all of ``training_filter_split``,
            ``validation_filter_split`` and
            ``test_filter_split`` must be provided.
            Supported only for unstructured Datasets.

        Args:
            dataset (datasets.ImageDataset): Required. The dataset within the same
              Project from which data will be used to train the Model. The Dataset
              must use schema compatible with Model being trained, and what is
              compatible should be described in the used TrainingPipeline's
              [training_task_definition]
              [google.cloud.aiplatform.v1beta1.TrainingPipeline.training_task_definition].
              For tabular Datasets, all their data is exported to training, to pick
              and choose from.
            base_model: Optional[models.Model] = None Optional. Only permitted for
              Image Classification models. If it is specified, the new model will be
              trained based on the `base` model. Otherwise, the new model will be
              trained from scratch. The `base` model must be in the same Project and
              Location as the new Model to train, and have the same model_type.
            incremental_train_base_model: Optional[models.Model] = None Optional for
              both Image Classification and Object detection models, to
              incrementally train a new model using an existing model as the
              starting point, with a reduced training time. If not specified, the
              new model will be trained from scratch. The `base` model must be in
              the same Project and Location as the new Model to train, and have the
              same prediction_type and model_type.
            model_id (str): Optional. The ID to use for the Model produced by this
              job, which will become the final component of the model resource name.
              This value may be up to 63 characters, and valid characters are
              `[a-z0-9_-]`. The first character cannot be a number or hyphen.
            parent_model (str): Optional. The resource name or model ID of an
              existing model. The new model uploaded by this job will be a version
              of `parent_model`.  Only set this field when training a new version of
              an existing model.
            is_default_version (bool): Optional. When set to True, the newly
              uploaded model version will automatically have alias "default"
              included. Subsequent uses of the model produced by this job without a
              version specified will use this "default" version.  When set to False,
              the "default" alias will not be moved. Actions targeting the model
              version produced by this job will need to specifically reference this
              version by ID or alias.  New model uploads, i.e. version 1, will
              always be "default" aliased.
            model_version_aliases (Sequence[str]): Optional. User provided version
              aliases so that the model version uploaded by this job can be
              referenced via alias instead of auto-generated version ID. A default
              version alias will be created for the first version of the model.  The
              format is [a-z][a-zA-Z0-9-]{0,126}[a-z0-9]
            model_version_description (str): Optional. The description of the model
              version being uploaded by this job.
            training_fraction_split (float): Optional. The fraction of the input
              data that is to be used to train the Model. This is ignored if Dataset
              is not provided.
            validation_fraction_split (float): Optional. The fraction of the input
              data that is to be used to validate the Model. This is ignored if
              Dataset is not provided.
            test_fraction_split (float): Optional. The fraction of the input data
              that is to be used to evaluate the Model. This is ignored if Dataset
              is not provided.
            training_filter_split (str): Optional. A filter on DataItems of the
              Dataset. DataItems that match this filter are used to train the Model.
              A filter with same syntax as the one used in
              DatasetService.ListDataItems may be used. If a single DataItem is
              matched by more than one of the FilterSplit filters, then it is
              assigned to the first set that applies to it in the training,
              validation, test order. This is ignored if Dataset is not provided.
            validation_filter_split (str): Optional. A filter on DataItems of the
              Dataset. DataItems that match this filter are used to validate the
              Model. A filter with same syntax as the one used in
              DatasetService.ListDataItems may be used. If a single DataItem is
              matched by more than one of the FilterSplit filters, then it is
              assigned to the first set that applies to it in the training,
              validation, test order. This is ignored if Dataset is not provided.
            test_filter_split (str): Optional. A filter on DataItems of the Dataset.
              DataItems that match this filter are used to test the Model. A filter
              with same syntax as the one used in DatasetService.ListDataItems may
              be used. If a single DataItem is matched by more than one of the
              FilterSplit filters, then it is assigned to the first set that applies
              to it in the training, validation, test order. This is ignored if
              Dataset is not provided.
            budget_milli_node_hours (int): Optional. The train budget of creating
              this Model, expressed in milli node hours i.e. 1,000 value in this
              field means 1 node hour. The training cost of the model will not
              exceed this budget. The final cost will be attempted to be close to
              the budget, though may end up being (even) noticeably smaller - at the
              backend's discretion. This especially may happen when further model
              training ceases to provide any improvements. If the budget is set to a
              value known to be insufficient to train a Model for the given training
              set, the training won't be attempted and will error. The minimum value
              is 1000 and the maximum is 72000.
            model_display_name (str): Optional. The display name of the managed
              Vertex AI Model. The name can be up to 128 characters long and can be
              consist of any UTF-8 characters. If a `base_model` was provided, the
              display_name in the base_model will be overritten with this value. If
              not provided upon creation, the job's display_name is used.
            model_labels (Dict[str, str]): Optional. The labels with user-defined
              metadata to organize your Models. Label keys and values can be no
              longer than 64 characters (Unicode codepoints), can only contain
              lowercase letters, numeric characters, underscores and dashes.
              International characters are allowed. See https://goo.gl/xmQnxf for
              more information and examples of labels.
            disable_early_stopping (bool): Required. If true, the entire budget is
              used. This disables the early stopping feature. By default, the early
              stopping feature is enabled, which means that training might stop
              before the entire training budget has been used, if further training
              does no longer brings significant improvement to the model.
            sync (bool): Whether to execute this method synchronously. If False,
              this method will be executed in concurrent Future and any downstream
              object will be immediately returned and synced when the Future has
              completed.
            create_request_timeout (float): Optional. The timeout for the create
              request in seconds.

        Returns:
            model: The trained Vertex AI Model resource or None if training did not
                produce a Vertex AI Model.
        """

        # Retrieve the objective-specific training task schema based on prediction_type
        training_task_definition = getattr(
            schema.training_job.definition, f"automl_image_{self._prediction_type}"
        )

        training_task_inputs_dict = {
            # required inputs
            "modelType": self._model_type,
            "budgetMilliNodeHours": budget_milli_node_hours,
            # optional inputs
            "disableEarlyStopping": disable_early_stopping,
        }

        if self._prediction_type == "classification":
            training_task_inputs_dict["multiLabel"] = self._multi_label

        # gca Model to be trained
        model_tbt = gca_model.Model(encryption_spec=self._model_encryption_spec)

        model_tbt.display_name = model_display_name or self._display_name
        model_tbt.labels = model_labels or self._labels

        if base_model:
            # Use provided base_model to pass to model_to_upload causing the
            # description and labels from base_model to be passed onto the new model
            model_tbt.description = getattr(base_model._gca_resource, "description")
            model_tbt.labels = getattr(base_model._gca_resource, "labels")

            # Set ID of Vertex AI Model to base this training job off of
            training_task_inputs_dict["baseModelId"] = base_model.name

        if incremental_train_base_model:
            training_task_inputs_dict[
                "uptrainBaseModelId"
            ] = incremental_train_base_model.name

        tunable_parameter_dict: Dict[str, any] = {}

        if self._checkpoint_name:
            tunable_parameter_dict["checkpointName"] = self._checkpoint_name

        if self._study_spec:
            tunable_parameter_dict["studySpec"] = json_format.MessageToDict(
                self._study_spec._pb
            )

        if self._trainer_config:
            tunable_parameter_dict["trainerConfig"] = self._trainer_config

        if tunable_parameter_dict:
            training_task_inputs_dict["tunableParameter"] = tunable_parameter_dict

        return self._run_job(
            training_task_definition=training_task_definition,
            training_task_inputs=training_task_inputs_dict,
            dataset=dataset,
            training_fraction_split=training_fraction_split,
            validation_fraction_split=validation_fraction_split,
            test_fraction_split=test_fraction_split,
            training_filter_split=training_filter_split,
            validation_filter_split=validation_filter_split,
            test_filter_split=test_filter_split,
            model=model_tbt,
            model_id=model_id,
            parent_model=parent_model,
            is_default_version=is_default_version,
            model_version_aliases=model_version_aliases,
            model_version_description=model_version_description,
            create_request_timeout=create_request_timeout,
        )

    @property
    def _model_upload_fail_string(self) -> str:
        """Helper property for model upload failure."""
        return (
            f"AutoML Image Training Pipeline {self.resource_name} is not "
            "configured to upload a Model."
        )


class CustomPythonPackageTrainingJob(_CustomTrainingJob):
    """Class to launch a Custom Training Job in Vertex AI using a Python
    Package.

    Use the `CustomPythonPackageTrainingJob` class to use a Python package to
    launch a custom training pipeline in Vertex AI. For an example of how to use
    the `CustomPythonPackageTrainingJob` class, see the tutorial in the [Custom
    training using Python package, managed text dataset, and TensorFlow serving
    container](https://github.com/GoogleCloudPlatform/vertex-ai-samples/blob/main/notebooks/official/sdk/SDK_Custom_Training_Python_Package_Managed_Text_Dataset_Tensorflow_Serving_Container.ipynb)
    notebook.

    """

    def __init__(
        self,
        # TODO(b/223262536): Make display_name parameter fully optional in next major release
        display_name: str,
        python_package_gcs_uri: Union[str, List[str]],
        python_module_name: str,
        container_uri: str,
        model_serving_container_image_uri: Optional[str] = None,
        model_serving_container_predict_route: Optional[str] = None,
        model_serving_container_health_route: Optional[str] = None,
        model_serving_container_command: Optional[Sequence[str]] = None,
        model_serving_container_args: Optional[Sequence[str]] = None,
        model_serving_container_environment_variables: Optional[Dict[str, str]] = None,
        model_serving_container_ports: Optional[Sequence[int]] = None,
        model_description: Optional[str] = None,
        model_instance_schema_uri: Optional[str] = None,
        model_parameters_schema_uri: Optional[str] = None,
        model_prediction_schema_uri: Optional[str] = None,
        explanation_metadata: Optional[explain.ExplanationMetadata] = None,
        explanation_parameters: Optional[explain.ExplanationParameters] = None,
        project: Optional[str] = None,
        location: Optional[str] = None,
        credentials: Optional[auth_credentials.Credentials] = None,
        labels: Optional[Dict[str, str]] = None,
        training_encryption_spec_key_name: Optional[str] = None,
        model_encryption_spec_key_name: Optional[str] = None,
        staging_bucket: Optional[str] = None,
    ):
        """Constructs a Custom Training Job from a Python Package.

        A class to launch a custom training job in Vertex AI using a Python
        Package.

        Use the `CustomPythonPackageTrainingJob` class to use a Python package
        to launch a custom training pipeline in Vertex AI. For an example of how
        to use the `CustomPythonPackageTrainingJob` class, see the tutorial in
        the [Custom training using Python package, managed text dataset, and
        TensorFlow serving
        container](https://github.com/GoogleCloudPlatform/vertex-ai-samples/blob/main/notebooks/official/sdk/SDK_Custom_Training_Python_Package_Managed_Text_Dataset_Tensorflow_Serving_Container.ipynb)
        notebook.

        job = aiplatform.CustomPythonPackageTrainingJob(
            display_name='test-train',
            python_package_gcs_uri='gs://my-bucket/my-python-package.tar.gz',
            python_module_name='my-training-python-package.task',
            container_uri='gcr.io/cloud-aiplatform/training/tf-cpu.2-2:latest',
            model_serving_container_image_uri='gcr.io/my-trainer/serving:1',
            model_serving_container_predict_route='predict',
            model_serving_container_health_route='metadata,
            labels={'key': 'value'},
        )

        Usage with Dataset:

            ds = aiplatform.TabularDataset(
                'projects/my-project/locations/us-central1/datasets/12345'
            )

            job.run(
                ds,
                replica_count=1,
                model_display_name='my-trained-model',
                model_labels={'key': 'value'},
            )

        Usage without Dataset:

            job.run(
                replica_count=1,
                model_display_name='my-trained-model',
                model_labels={'key': 'value'},
            )

        To ensure your model gets saved in Vertex AI, write your saved model to
        os.environ["AIP_MODEL_DIR"] in your provided training script.

        Args:
            display_name (str):
                Required. The user-defined name of this TrainingPipeline.
            python_package_gcs_uri (Union[str, List[str]]):
                Required. GCS location of the training python package.
                Could be a string for single package or a list of string for
                multiple packages.
            python_module_name (str):
                Required. The module name of the training python package.
            container_uri (str):
                Required. Uri of the training container image in the GCR.
            model_serving_container_image_uri (str):
                Optional. If the training produces a managed Vertex AI Model,
                the URI of the model serving container suitable for serving the
                model produced by the training script.
            model_serving_container_predict_route (str):
                Optional. If the training produces a managed Vertex AI Model,
                an HTTP path to send prediction requests to the container,
                and which must be supported by it. If not specified a default
                HTTP path will be used by Vertex AI.
            model_serving_container_health_route (str):
                Optional. If the training produces a managed Vertex AI Model,
                an HTTP path to send health check requests to the container,
                and which must be supported by it. If not specified a standard
                HTTP path will be used by AI Platform.
            model_serving_container_command (Sequence[str]):
                Optional. The command with which the container is run. Not executed within a
                shell. The Docker image's ENTRYPOINT is used if this is not provided.
                Variable references $(VAR_NAME) are expanded using the container's
                environment. If a variable cannot be resolved, the reference in the
                input string will be unchanged. The $(VAR_NAME) syntax can be escaped
                with a double $$, ie: $$(VAR_NAME). Escaped references will never be
                expanded, regardless of whether the variable exists or not.
            model_serving_container_args (Sequence[str]):
                Optional. The arguments to the command. The Docker image's CMD is used if this
                is not provided. Variable references $(VAR_NAME) are expanded using the
                container's environment. If a variable cannot be resolved, the reference
                in the input string will be unchanged. The $(VAR_NAME) syntax can be
                escaped with a double $$, ie: $$(VAR_NAME). Escaped references will
                never be expanded, regardless of whether the variable exists or not.
            model_serving_container_environment_variables (Dict[str, str]):
                Optional. The environment variables that are to be present in the container.
                Should be a dictionary where keys are environment variable names
                and values are environment variable values for those names.
            model_serving_container_ports (Sequence[int]):
                Optional. Declaration of ports that are exposed by the container.
                This field is primarily informational, it gives Vertex AI information
                about the network connections the container uses. Listing or not
                a port here has no impact on whether the port is actually exposed,
                any port listening on the default "0.0.0.0" address inside a
                container will be accessible from the network.
            model_description (str):
                Optional. The description of the Model.
            model_instance_schema_uri (str):
                Optional. Points to a YAML file stored on Google Cloud
                Storage describing the format of a single instance, which
                are used in
                ``PredictRequest.instances``,
                ``ExplainRequest.instances``
                and
                ``BatchPredictionJob.input_config``.
                The schema is defined as an OpenAPI 3.0.2 `Schema
                Object <https://tinyurl.com/y538mdwt#schema-object>`__.
                AutoML Models always have this field populated by AI
                Platform. Note: The URI given on output will be immutable
                and probably different, including the URI scheme, than the
                one given on input. The output URI will point to a location
                where the user only has a read access.
            model_parameters_schema_uri (str):
                Optional. Points to a YAML file stored on Google Cloud
                Storage describing the parameters of prediction and
                explanation via
                ``PredictRequest.parameters``,
                ``ExplainRequest.parameters``
                and
                ``BatchPredictionJob.model_parameters``.
                The schema is defined as an OpenAPI 3.0.2 `Schema
                Object <https://tinyurl.com/y538mdwt#schema-object>`__.
                AutoML Models always have this field populated by AI
                Platform, if no parameters are supported it is set to an
                empty string. Note: The URI given on output will be
                immutable and probably different, including the URI scheme,
                than the one given on input. The output URI will point to a
                location where the user only has a read access.
            model_prediction_schema_uri (str):
                Optional. Points to a YAML file stored on Google Cloud
                Storage describing the format of a single prediction
                produced by this Model, which are returned via
                ``PredictResponse.predictions``,
                ``ExplainResponse.explanations``,
                and
                ``BatchPredictionJob.output_config``.
                The schema is defined as an OpenAPI 3.0.2 `Schema
                Object <https://tinyurl.com/y538mdwt#schema-object>`__.
                AutoML Models always have this field populated by AI
                Platform. Note: The URI given on output will be immutable
                and probably different, including the URI scheme, than the
                one given on input. The output URI will point to a location
                where the user only has a read access.
            explanation_metadata (explain.ExplanationMetadata):
                Optional. Metadata describing the Model's input and output for
                explanation. `explanation_metadata` is optional while
                `explanation_parameters` must be specified when used.
                For more details, see `Ref docs <http://tinyurl.com/1igh60kt>`
            explanation_parameters (explain.ExplanationParameters):
                Optional. Parameters to configure explaining for Model's
                predictions.
                For more details, see `Ref docs <http://tinyurl.com/1an4zake>`
            project (str):
                Project to run training in. Overrides project set in aiplatform.init.
            location (str):
                Location to run training in. Overrides location set in aiplatform.init.
            credentials (auth_credentials.Credentials):
                Custom credentials to use to run call training service. Overrides
                credentials set in aiplatform.init.
            labels (Dict[str, str]):
                Optional. The labels with user-defined metadata to
                organize TrainingPipelines.
                Label keys and values can be no longer than 64
                characters (Unicode codepoints), can only
                contain lowercase letters, numeric characters,
                underscores and dashes. International characters
                are allowed.
                See https://goo.gl/xmQnxf for more information
                and examples of labels.
            training_encryption_spec_key_name (Optional[str]):
                Optional. The Cloud KMS resource identifier of the customer
                managed encryption key used to protect the training pipeline. Has the
                form:
                ``projects/my-project/locations/my-region/keyRings/my-kr/cryptoKeys/my-key``.
                The key needs to be in the same region as where the compute
                resource is created.

                If set, this TrainingPipeline will be secured by this key.

                Note: Model trained by this TrainingPipeline is also secured
                by this key if ``model_to_upload`` is not set separately.

                Overrides encryption_spec_key_name set in aiplatform.init.
            model_encryption_spec_key_name (Optional[str]):
                Optional. The Cloud KMS resource identifier of the customer
                managed encryption key used to protect the model. Has the
                form:
                ``projects/my-project/locations/my-region/keyRings/my-kr/cryptoKeys/my-key``.
                The key needs to be in the same region as where the compute
                resource is created.

                If set, the trained Model will be secured by this key.

                Overrides encryption_spec_key_name set in aiplatform.init.
            staging_bucket (str):
                Optional. Bucket used to stage source and training artifacts. Overrides
                staging_bucket set in aiplatform.init.
        """
        if not display_name:
            display_name = self.__class__._generate_display_name()
        super().__init__(
            display_name=display_name,
            project=project,
            location=location,
            credentials=credentials,
            labels=labels,
            training_encryption_spec_key_name=training_encryption_spec_key_name,
            model_encryption_spec_key_name=model_encryption_spec_key_name,
            container_uri=container_uri,
            model_instance_schema_uri=model_instance_schema_uri,
            model_parameters_schema_uri=model_parameters_schema_uri,
            model_prediction_schema_uri=model_prediction_schema_uri,
            model_serving_container_environment_variables=model_serving_container_environment_variables,
            model_serving_container_ports=model_serving_container_ports,
            model_serving_container_image_uri=model_serving_container_image_uri,
            model_serving_container_command=model_serving_container_command,
            model_serving_container_args=model_serving_container_args,
            model_serving_container_predict_route=model_serving_container_predict_route,
            model_serving_container_health_route=model_serving_container_health_route,
            model_description=model_description,
            explanation_metadata=explanation_metadata,
            explanation_parameters=explanation_parameters,
            staging_bucket=staging_bucket,
        )

        if isinstance(python_package_gcs_uri, str):
            self._package_gcs_uri = [python_package_gcs_uri]
        elif isinstance(python_package_gcs_uri, list):
            self._package_gcs_uri = python_package_gcs_uri
        else:
            raise ValueError("'python_package_gcs_uri' must be a string or list.")
        self._python_module = python_module_name

    def run(
        self,
        dataset: Optional[
            Union[
                datasets.ImageDataset,
                datasets.TabularDataset,
                datasets.TextDataset,
                datasets.VideoDataset,
            ]
        ] = None,
        annotation_schema_uri: Optional[str] = None,
        model_display_name: Optional[str] = None,
        model_labels: Optional[Dict[str, str]] = None,
        model_id: Optional[str] = None,
        parent_model: Optional[str] = None,
        is_default_version: Optional[bool] = True,
        model_version_aliases: Optional[Sequence[str]] = None,
        model_version_description: Optional[str] = None,
        base_output_dir: Optional[str] = None,
        service_account: Optional[str] = None,
        network: Optional[str] = None,
        bigquery_destination: Optional[str] = None,
        args: Optional[List[Union[str, float, int]]] = None,
        environment_variables: Optional[Dict[str, str]] = None,
        replica_count: int = 1,
        machine_type: str = "n1-standard-4",
        accelerator_type: str = "ACCELERATOR_TYPE_UNSPECIFIED",
        accelerator_count: int = 0,
        boot_disk_type: str = "pd-ssd",
        boot_disk_size_gb: int = 100,
        reduction_server_replica_count: int = 0,
        reduction_server_machine_type: Optional[str] = None,
        reduction_server_container_uri: Optional[str] = None,
        training_fraction_split: Optional[float] = None,
        validation_fraction_split: Optional[float] = None,
        test_fraction_split: Optional[float] = None,
        training_filter_split: Optional[str] = None,
        validation_filter_split: Optional[str] = None,
        test_filter_split: Optional[str] = None,
        predefined_split_column_name: Optional[str] = None,
        timestamp_split_column_name: Optional[str] = None,
        timeout: Optional[int] = None,
        restart_job_on_worker_restart: bool = False,
        enable_web_access: bool = False,
        enable_dashboard_access: bool = False,
        tensorboard: Optional[str] = None,
        sync=True,
        create_request_timeout: Optional[float] = None,
        disable_retries: bool = False,
        persistent_resource_id: Optional[str] = None,
        tpu_topology: Optional[str] = None,
    ) -> Optional[models.Model]:
        """Runs the custom training job.

        Distributed Training Support:
        If replica count = 1 then one chief replica will be provisioned. If
        replica_count > 1 the remainder will be provisioned as a worker replica pool.
        ie: replica_count = 10 will result in 1 chief and 9 workers
        All replicas have same machine_type, accelerator_type, and accelerator_count

        If training on a Vertex AI dataset, you can use one of the following split configurations:
            Data fraction splits:
            Any of ``training_fraction_split``, ``validation_fraction_split`` and
            ``test_fraction_split`` may optionally be provided, they must sum to up to 1. If
            the provided ones sum to less than 1, the remainder is assigned to sets as
            decided by Vertex AI. If none of the fractions are set, by default roughly 80%
            of data will be used for training, 10% for validation, and 10% for test.

            Data filter splits:
            Assigns input data to training, validation, and test sets
            based on the given filters, data pieces not matched by any
            filter are ignored. Currently only supported for Datasets
            containing DataItems.
            If any of the filters in this message are to match nothing, then
            they can be set as '-' (the minus sign).
            If using filter splits, all of ``training_filter_split``, ``validation_filter_split`` and
            ``test_filter_split`` must be provided.
            Supported only for unstructured Datasets.

            Predefined splits:
            Assigns input data to training, validation, and test sets based on the value of a provided key.
            If using predefined splits, ``predefined_split_column_name`` must be provided.
            Supported only for tabular Datasets.

            Timestamp splits:
            Assigns input data to training, validation, and test sets
            based on a provided timestamps. The youngest data pieces are
            assigned to training set, next to validation set, and the oldest
            to the test set.
            Supported only for tabular Datasets.

        Args:
            dataset (Union[datasets.ImageDataset,datasets.TabularDataset,datasets.TextDataset,datasets.VideoDataset,]):
                Vertex AI to fit this training against. Custom training script should
                retrieve datasets through passed in environment variables uris:

                os.environ["AIP_TRAINING_DATA_URI"]
                os.environ["AIP_VALIDATION_DATA_URI"]
                os.environ["AIP_TEST_DATA_URI"]

                Additionally the dataset format is passed in as:

                os.environ["AIP_DATA_FORMAT"]
            annotation_schema_uri (str):
                Google Cloud Storage URI points to a YAML file describing
                annotation schema. The schema is defined as an OpenAPI 3.0.2
                [Schema Object](https://github.com/OAI/OpenAPI-Specification/blob/main/versions/3.0.2.md#schema-object) The schema files
                that can be used here are found in
                gs://google-cloud-aiplatform/schema/dataset/annotation/,
                note that the chosen schema must be consistent with
                ``metadata``
                of the Dataset specified by
                ``dataset_id``.

                Only Annotations that both match this schema and belong to
                DataItems not ignored by the split method are used in
                respectively training, validation or test role, depending on
                the role of the DataItem they are on.

                When used in conjunction with
                ``annotations_filter``,
                the Annotations used for training are filtered by both
                ``annotations_filter``
                and
                ``annotation_schema_uri``.
            model_display_name (str):
                If the script produces a managed Vertex AI Model. The display name of
                the Model. The name can be up to 128 characters long and can be consist
                of any UTF-8 characters.

                If not provided upon creation, the job's display_name is used.
            model_labels (Dict[str, str]):
                Optional. The labels with user-defined metadata to
                organize your Models.
                Label keys and values can be no longer than 64
                characters (Unicode codepoints), can only
                contain lowercase letters, numeric characters,
                underscores and dashes. International characters
                are allowed.
                See https://goo.gl/xmQnxf for more information
                and examples of labels.
            model_id (str):
                Optional. The ID to use for the Model produced by this job,
                which will become the final component of the model resource name.
                This value may be up to 63 characters, and valid characters
                are `[a-z0-9_-]`. The first character cannot be a number or hyphen.
            parent_model (str):
                Optional. The resource name or model ID of an existing model.
                The new model uploaded by this job will be a version of `parent_model`.

                Only set this field when training a new version of an existing model.
            is_default_version (bool):
                Optional. When set to True, the newly uploaded model version will
                automatically have alias "default" included. Subsequent uses of
                the model produced by this job without a version specified will
                use this "default" version.

                When set to False, the "default" alias will not be moved.
                Actions targeting the model version produced by this job will need
                to specifically reference this version by ID or alias.

                New model uploads, i.e. version 1, will always be "default" aliased.
            model_version_aliases (Sequence[str]):
                Optional. User provided version aliases so that the model version
                uploaded by this job can be referenced via alias instead of
                auto-generated version ID. A default version alias will be created
                for the first version of the model.

                The format is [a-z][a-zA-Z0-9-]{0,126}[a-z0-9]
            model_version_description (str):
               Optional. The description of the model version being uploaded by this job.
            base_output_dir (str):
                GCS output directory of job. If not provided a
                timestamped directory in the staging directory will be used.

                Vertex AI sets the following environment variables when it runs your training code:

                -  AIP_MODEL_DIR: a Cloud Storage URI of a directory intended for saving model artifacts, i.e. <base_output_dir>/model/
                -  AIP_CHECKPOINT_DIR: a Cloud Storage URI of a directory intended for saving checkpoints, i.e. <base_output_dir>/checkpoints/
                -  AIP_TENSORBOARD_LOG_DIR: a Cloud Storage URI of a directory intended for saving TensorBoard logs, i.e. <base_output_dir>/logs/

            service_account (str):
                Specifies the service account for workload run-as account.
                Users submitting jobs must have act-as permission on this run-as account.
                If not specified, uses the service account set in aiplatform.init.
            network (str):
                The full name of the Compute Engine network to which the job
                should be peered. For example, projects/12345/global/networks/myVPC.
                Private services access must already be configured for the network.
                If left unspecified, the network set in aiplatform.init will be used.
                Otherwise, the job is not peered with any network.
            bigquery_destination (str):
                Provide this field if `dataset` is a BigQuery dataset.
                The BigQuery project location where the training data is to
                be written to. In the given project a new dataset is created
                with name
                ``dataset_<dataset-id>_<annotation-type>_<timestamp-of-training-call>``
                where timestamp is in YYYY_MM_DDThh_mm_ss_sssZ format. All
                training input data will be written into that dataset. In
                the dataset three tables will be created, ``training``,
                ``validation`` and ``test``.

                -  AIP_DATA_FORMAT = "bigquery".
                -  AIP_TRAINING_DATA_URI ="bigquery_destination.dataset_*.training"
                -  AIP_VALIDATION_DATA_URI = "bigquery_destination.dataset_*.validation"
                -  AIP_TEST_DATA_URI = "bigquery_destination.dataset_*.test"
            args (List[Unions[str, int, float]]):
                Command line arguments to be passed to the Python script.
            environment_variables (Dict[str, str]):
                Environment variables to be passed to the container.
                Should be a dictionary where keys are environment variable names
                and values are environment variable values for those names.
                At most 10 environment variables can be specified.
                The Name of the environment variable must be unique.

                environment_variables = {
                    'MY_KEY': 'MY_VALUE'
                }
            replica_count (int):
                The number of worker replicas. If replica count = 1 then one chief
                replica will be provisioned. If replica_count > 1 the remainder will be
                provisioned as a worker replica pool.
            machine_type (str):
                The type of machine to use for training.
            accelerator_type (str):
                Hardware accelerator type. One of ACCELERATOR_TYPE_UNSPECIFIED,
                NVIDIA_TESLA_K80, NVIDIA_TESLA_P100, NVIDIA_TESLA_V100, NVIDIA_TESLA_P4,
                NVIDIA_TESLA_T4
            accelerator_count (int):
                The number of accelerators to attach to a worker replica.
            boot_disk_type (str):
                Type of the boot disk, default is `pd-ssd`.
                Valid values: `pd-ssd` (Persistent Disk Solid State Drive) or
                `pd-standard` (Persistent Disk Hard Disk Drive).
            boot_disk_size_gb (int):
                Size in GB of the boot disk, default is 100GB.
                boot disk size must be within the range of [100, 64000].
            reduction_server_replica_count (int):
                The number of reduction server replicas, default is 0.
            reduction_server_machine_type (str):
                Optional. The type of machine to use for reduction server.
            reduction_server_container_uri (str):
                Optional. The Uri of the reduction server container image.
                See details: https://cloud.google.com/vertex-ai/docs/training/distributed-training#reduce_training_time_with_reduction_server
            training_fraction_split (float):
                Optional. The fraction of the input data that is to be used to train
                the Model. This is ignored if Dataset is not provided.
            validation_fraction_split (float):
                Optional. The fraction of the input data that is to be used to validate
                the Model. This is ignored if Dataset is not provided.
            test_fraction_split (float):
                Optional. The fraction of the input data that is to be used to evaluate
                the Model. This is ignored if Dataset is not provided.
            training_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to train the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            validation_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to validate the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            test_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to test the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            predefined_split_column_name (str):
                Optional. The key is a name of one of the Dataset's data
                columns. The value of the key (either the label's value or
                value in the column) must be one of {``training``,
                ``validation``, ``test``}, and it defines to which set the
                given piece of data is assigned. If for a piece of data the
                key is not present or has an invalid value, that piece is
                ignored by the pipeline.

                Supported only for tabular and time series Datasets.
            timestamp_split_column_name (str):
                Optional. The key is a name of one of the Dataset's data
                columns. The value of the key values of the key (the values in
                the column) must be in RFC 3339 `date-time` format, where
                `time-offset` = `"Z"` (e.g. 1985-04-12T23:20:50.52Z). If for a
                piece of data the key is not present or has an invalid value,
                that piece is ignored by the pipeline.

                Supported only for tabular and time series Datasets.
            timeout (int):
                The maximum job running time in seconds. The default is 7 days.
            restart_job_on_worker_restart (bool):
                Restarts the entire CustomJob if a worker
                gets restarted. This feature can be used by
                distributed training jobs that are not resilient
                to workers leaving and joining a job.
            enable_web_access (bool):
                Whether you want Vertex AI to enable interactive shell access
                to training containers.
                https://cloud.google.com/vertex-ai/docs/training/monitor-debug-interactive-shell
            enable_dashboard_access (bool):
                Whether you want Vertex AI to enable access to the customized dashboard
                to training containers.
            tensorboard (str):
                Optional. The name of a Vertex AI
                [Tensorboard][google.cloud.aiplatform.v1beta1.Tensorboard]
                resource to which this CustomJob will upload Tensorboard
                logs. Format:
                ``projects/{project}/locations/{location}/tensorboards/{tensorboard}``

                The training script should write Tensorboard to following Vertex AI environment
                variable:

                AIP_TENSORBOARD_LOG_DIR

                `service_account` is required with provided `tensorboard`.
                For more information on configuring your service account please visit:
                https://cloud.google.com/vertex-ai/docs/experiments/tensorboard-training
            sync (bool):
                Whether to execute this method synchronously. If False, this method
                will be executed in concurrent Future and any downstream object will
                be immediately returned and synced when the Future has completed.
            create_request_timeout (float):
                Optional. The timeout for the create request in seconds.
            disable_retries (bool):
                Indicates if the job should retry for internal errors after the
                job starts running. If True, overrides
                `restart_job_on_worker_restart` to False.
            persistent_resource_id (str):
                Optional. The ID of the PersistentResource in the same Project
                and Location. If this is specified, the job will be run on
                existing machines held by the PersistentResource instead of
                on-demand short-live machines. The network, CMEK, and node pool
                configs on the job should be consistent with those on the
                PersistentResource, otherwise, the job will be rejected.
            tpu_topology (str):
                Optional. Specifies the tpu topology to be used for
                TPU training job. This field is required for TPU v5 versions. For
                details on the TPU topology, refer to
                https://cloud.google.com/tpu/docs/v5e#tpu-v5e-config. The topology
                must be a supported value for the TPU machine type.

        Returns:
            model: The trained Vertex AI Model resource or None if training did not
                produce a Vertex AI Model.
        """
        network = network or initializer.global_config.network
        service_account = service_account or initializer.global_config.service_account

        worker_pool_specs, managed_model = self._prepare_and_validate_run(
            model_display_name=model_display_name,
            model_labels=model_labels,
            replica_count=replica_count,
            machine_type=machine_type,
            accelerator_count=accelerator_count,
            accelerator_type=accelerator_type,
            boot_disk_type=boot_disk_type,
            boot_disk_size_gb=boot_disk_size_gb,
            reduction_server_replica_count=reduction_server_replica_count,
            reduction_server_machine_type=reduction_server_machine_type,
            tpu_topology=tpu_topology,
        )

        return self._run(
            dataset=dataset,
            annotation_schema_uri=annotation_schema_uri,
            worker_pool_specs=worker_pool_specs,
            managed_model=managed_model,
            model_id=model_id,
            parent_model=parent_model,
            is_default_version=is_default_version,
            model_version_aliases=model_version_aliases,
            model_version_description=model_version_description,
            args=args,
            environment_variables=environment_variables,
            base_output_dir=base_output_dir,
            service_account=service_account,
            network=network,
            training_fraction_split=training_fraction_split,
            validation_fraction_split=validation_fraction_split,
            test_fraction_split=test_fraction_split,
            training_filter_split=training_filter_split,
            validation_filter_split=validation_filter_split,
            test_filter_split=test_filter_split,
            predefined_split_column_name=predefined_split_column_name,
            timestamp_split_column_name=timestamp_split_column_name,
            bigquery_destination=bigquery_destination,
            timeout=timeout,
            restart_job_on_worker_restart=restart_job_on_worker_restart,
            enable_web_access=enable_web_access,
            enable_dashboard_access=enable_dashboard_access,
            tensorboard=tensorboard,
            reduction_server_container_uri=reduction_server_container_uri
            if reduction_server_replica_count > 0
            else None,
            sync=sync,
            create_request_timeout=create_request_timeout,
            disable_retries=disable_retries,
            persistent_resource_id=persistent_resource_id,
        )

    @base.optional_sync(construct_object_on_arg="managed_model")
    def _run(
        self,
        dataset: Optional[
            Union[
                datasets.ImageDataset,
                datasets.TabularDataset,
                datasets.TextDataset,
                datasets.VideoDataset,
            ]
        ],
        annotation_schema_uri: Optional[str],
        worker_pool_specs: worker_spec_utils._DistributedTrainingSpec,
        managed_model: Optional[gca_model.Model] = None,
        model_id: Optional[str] = None,
        parent_model: Optional[str] = None,
        is_default_version: Optional[bool] = True,
        model_version_aliases: Optional[Sequence[str]] = None,
        model_version_description: Optional[str] = None,
        args: Optional[List[Union[str, float, int]]] = None,
        environment_variables: Optional[Dict[str, str]] = None,
        base_output_dir: Optional[str] = None,
        service_account: Optional[str] = None,
        network: Optional[str] = None,
        training_fraction_split: Optional[float] = None,
        validation_fraction_split: Optional[float] = None,
        test_fraction_split: Optional[float] = None,
        training_filter_split: Optional[str] = None,
        validation_filter_split: Optional[str] = None,
        test_filter_split: Optional[str] = None,
        predefined_split_column_name: Optional[str] = None,
        timestamp_split_column_name: Optional[str] = None,
        bigquery_destination: Optional[str] = None,
        timeout: Optional[int] = None,
        restart_job_on_worker_restart: bool = False,
        enable_web_access: bool = False,
        enable_dashboard_access: bool = False,
        tensorboard: Optional[str] = None,
        reduction_server_container_uri: Optional[str] = None,
        sync=True,
        create_request_timeout: Optional[float] = None,
        disable_retries: bool = False,
        persistent_resource_id: Optional[str] = None,
    ) -> Optional[models.Model]:
        """Packages local script and launches training_job.

        Args:
            dataset (
                Union[
                    datasets.ImageDataset,
                    datasets.TabularDataset,
                    datasets.TextDataset,
                    datasets.VideoDataset,
                ]
            ):
                Vertex AI to fit this training against.
            annotation_schema_uri (str):
                Google Cloud Storage URI points to a YAML file describing
                annotation schema.
            worker_pools_spec (worker_spec_utils._DistributedTrainingSpec):
                Worker pools pecs required to run job.
            managed_model (gca_model.Model):
                Model proto if this script produces a Managed Model.
            model_id (str):
                Optional. The ID to use for the Model produced by this job,
                which will become the final component of the model resource name.
                This value may be up to 63 characters, and valid characters
                are `[a-z0-9_-]`. The first character cannot be a number or hyphen.
            parent_model (str):
                Optional. The resource name or model ID of an existing model.
                The new model uploaded by this job will be a version of `parent_model`.

                Only set this field when training a new version of an existing model.
            is_default_version (bool):
                Optional. When set to True, the newly uploaded model version will
                automatically have alias "default" included. Subsequent uses of
                the model produced by this job without a version specified will
                use this "default" version.

                When set to False, the "default" alias will not be moved.
                Actions targeting the model version produced by this job will need
                to specifically reference this version by ID or alias.

                New model uploads, i.e. version 1, will always be "default" aliased.
            model_version_aliases (Sequence[str]):
                Optional. User provided version aliases so that the model version
                uploaded by this job can be referenced via alias instead of
                auto-generated version ID. A default version alias will be created
                for the first version of the model.

                The format is [a-z][a-zA-Z0-9-]{0,126}[a-z0-9]
            model_version_description (str):
               Optional. The description of the model version being uploaded by this job.
            args (List[Unions[str, int, float]]):
                Command line arguments to be passed to the Python script.
            environment_variables (Dict[str, str]):
                Environment variables to be passed to the container.
                Should be a dictionary where keys are environment variable names
                and values are environment variable values for those names.
                At most 10 environment variables can be specified.
                The Name of the environment variable must be unique.

                environment_variables = {
                    'MY_KEY': 'MY_VALUE'
                }
            base_output_dir (str):
                GCS output directory of job. If not provided a
                timestamped directory in the staging directory will be used.

                Vertex AI sets the following environment variables when it runs your training code:

                -  AIP_MODEL_DIR: a Cloud Storage URI of a directory intended for saving model artifacts, i.e. <base_output_dir>/model/
                -  AIP_CHECKPOINT_DIR: a Cloud Storage URI of a directory intended for saving checkpoints, i.e. <base_output_dir>/checkpoints/
                -  AIP_TENSORBOARD_LOG_DIR: a Cloud Storage URI of a directory intended for saving TensorBoard logs, i.e. <base_output_dir>/logs/

            service_account (str):
                Specifies the service account for workload run-as account.
                Users submitting jobs must have act-as permission on this run-as account.
            network (str):
                The full name of the Compute Engine network to which the job
                should be peered. For example, projects/12345/global/networks/myVPC.
                Private services access must already be configured for the network.
                If left unspecified, the job is not peered with any network.
            training_fraction_split (float):
                Optional. The fraction of the input data that is to be used to train
                the Model. This is ignored if Dataset is not provided.
            validation_fraction_split (float):
                Optional. The fraction of the input data that is to be used to validate
                the Model. This is ignored if Dataset is not provided.
            test_fraction_split (float):
                Optional. The fraction of the input data that is to be used to evaluate
                the Model. This is ignored if Dataset is not provided.
            training_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to train the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            validation_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to validate the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            test_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to test the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            predefined_split_column_name (str):
                Optional. The key is a name of one of the Dataset's data
                columns. The value of the key (either the label's value or
                value in the column) must be one of {``training``,
                ``validation``, ``test``}, and it defines to which set the
                given piece of data is assigned. If for a piece of data the
                key is not present or has an invalid value, that piece is
                ignored by the pipeline.

                Supported only for tabular and time series Datasets.
            timestamp_split_column_name (str):
                Optional. The key is a name of one of the Dataset's data
                columns. The value of the key values of the key (the values in
                the column) must be in RFC 3339 `date-time` format, where
                `time-offset` = `"Z"` (e.g. 1985-04-12T23:20:50.52Z). If for a
                piece of data the key is not present or has an invalid value,
                that piece is ignored by the pipeline.

                Supported only for tabular and time series Datasets.
            timeout (int):
                The maximum job running time in seconds. The default is 7 days.
            restart_job_on_worker_restart (bool):
                Restarts the entire CustomJob if a worker
                gets restarted. This feature can be used by
                distributed training jobs that are not resilient
                to workers leaving and joining a job.
            enable_web_access (bool):
                Whether you want Vertex AI to enable interactive shell access
                to training containers.
                https://cloud.google.com/vertex-ai/docs/training/monitor-debug-interactive-shell
            enable_dashboard_access (bool):
                Whether you want Vertex AI to enable access to the customized dashboard
                to training containers.
            tensorboard (str):
                Optional. The name of a Vertex AI
                [Tensorboard][google.cloud.aiplatform.v1beta1.Tensorboard]
                resource to which this CustomJob will upload Tensorboard
                logs. Format:
                ``projects/{project}/locations/{location}/tensorboards/{tensorboard}``

                The training script should write Tensorboard to following Vertex AI environment
                variable:

                AIP_TENSORBOARD_LOG_DIR

                `service_account` is required with provided `tensorboard`.
                For more information on configuring your service account please visit:
                https://cloud.google.com/vertex-ai/docs/experiments/tensorboard-training
            reduction_server_container_uri (str):
                Optional. The Uri of the reduction server container image.
            sync (bool):
                Whether to execute this method synchronously. If False, this method
                will be executed in concurrent Future and any downstream object will
                be immediately returned and synced when the Future has completed.
            create_request_timeout (float):
                Optional. The timeout for the create request in seconds.
            disable_retries (bool):
                Indicates if the job should retry for internal errors after the
                job starts running. If True, overrides
                `restart_job_on_worker_restart` to False.
            persistent_resource_id (str):
                Optional. The ID of the PersistentResource in the same Project
                and Location. If this is specified, the job will be run on
                existing machines held by the PersistentResource instead of
                on-demand short-live machines. The network, CMEK, and node pool
                configs on the job should be consistent with those on the
                PersistentResource, otherwise, the job will be rejected.

        Returns:
            model: The trained Vertex AI Model resource or None if training did not
                produce a Vertex AI Model.
        """
        for spec_order, spec in enumerate(worker_pool_specs):

            if not spec:
                continue

            if (
                spec_order == worker_spec_utils._SPEC_ORDERS["server_spec"]
                and reduction_server_container_uri
            ):
                spec["container_spec"] = {
                    "image_uri": reduction_server_container_uri,
                }
            else:
                spec["python_package_spec"] = {
                    "executor_image_uri": self._container_uri,
                    "python_module": self._python_module,
                    "package_uris": self._package_gcs_uri,
                }

                if args:
                    spec["python_package_spec"]["args"] = args

                if environment_variables:
                    spec["python_package_spec"]["env"] = [
                        {"name": key, "value": value}
                        for key, value in environment_variables.items()
                    ]

        (
            training_task_inputs,
            base_output_dir,
        ) = self._prepare_training_task_inputs_and_output_dir(
            worker_pool_specs=worker_pool_specs,
            base_output_dir=base_output_dir,
            service_account=service_account,
            network=network,
            timeout=timeout,
            restart_job_on_worker_restart=restart_job_on_worker_restart,
            enable_web_access=enable_web_access,
            enable_dashboard_access=enable_dashboard_access,
            tensorboard=tensorboard,
            disable_retries=disable_retries,
            persistent_resource_id=persistent_resource_id,
        )

        model = self._run_job(
            training_task_definition=schema.training_job.definition.custom_task,
            training_task_inputs=training_task_inputs,
            dataset=dataset,
            annotation_schema_uri=annotation_schema_uri,
            training_fraction_split=training_fraction_split,
            validation_fraction_split=validation_fraction_split,
            test_fraction_split=test_fraction_split,
            training_filter_split=training_filter_split,
            validation_filter_split=validation_filter_split,
            test_filter_split=test_filter_split,
            predefined_split_column_name=predefined_split_column_name,
            timestamp_split_column_name=timestamp_split_column_name,
            model=managed_model,
            model_id=model_id,
            parent_model=parent_model,
            is_default_version=is_default_version,
            model_version_aliases=model_version_aliases,
            model_version_description=model_version_description,
            gcs_destination_uri_prefix=base_output_dir,
            bigquery_destination=bigquery_destination,
            create_request_timeout=create_request_timeout,
        )

        return model


class AutoMLVideoTrainingJob(_TrainingJob):

    _supported_training_schemas = (
        schema.training_job.definition.automl_video_classification,
        schema.training_job.definition.automl_video_object_tracking,
        schema.training_job.definition.automl_video_action_recognition,
    )

    def __init__(
        self,
        display_name: Optional[str] = None,
        prediction_type: str = "classification",
        model_type: str = "CLOUD",
        project: Optional[str] = None,
        location: Optional[str] = None,
        credentials: Optional[auth_credentials.Credentials] = None,
        labels: Optional[Dict[str, str]] = None,
        training_encryption_spec_key_name: Optional[str] = None,
        model_encryption_spec_key_name: Optional[str] = None,
    ):
        """Constructs a AutoML Video Training Job.

        Args:
            display_name (str):
                Required. The user-defined name of this TrainingPipeline.
            prediction_type (str):
                The type of prediction the Model is to produce, one of:
                    "classification" - A video classification model classifies shots
                        and segments in your videos according to your own defined labels.
                    "object_tracking" - A video object tracking model detects and tracks
                        multiple objects in shots and segments. You can use these
                        models to track objects in your videos according to your
                        own pre-defined, custom labels.
                    "action_recognition" - A video action recognition model pinpoints
                        the location of actions with short temporal durations (~1 second).
            model_type: str = "CLOUD"
                Required. One of the following:
                    "CLOUD" - available for "classification", "object_tracking" and "action_recognition"
                        A Model best tailored to be used within Google Cloud,
                        and which cannot be exported.
                    "MOBILE_VERSATILE_1" - available for "classification", "object_tracking" and "action_recognition"
                        A model that, in addition to being available within Google
                        Cloud, can also be exported (see ModelService.ExportModel)
                        as a TensorFlow or TensorFlow Lite model and used on a
                        mobile or edge device with afterwards.
                    "MOBILE_CORAL_VERSATILE_1" - available only for "object_tracking"
                        A versatile model that is meant to be exported (see
                        ModelService.ExportModel) and used on a Google Coral device.
                    "MOBILE_CORAL_LOW_LATENCY_1" - available only for "object_tracking"
                        A model that trades off quality for low latency, to be
                        exported (see ModelService.ExportModel) and used on a
                        Google Coral device.
                    "MOBILE_JETSON_VERSATILE_1" - available only for "object_tracking"
                        A versatile model that is meant to be exported (see
                        ModelService.ExportModel) and used on an NVIDIA Jetson device.
                    "MOBILE_JETSON_LOW_LATENCY_1" - available only for "object_tracking"
                        A model that trades off quality for low latency, to be
                        exported (see ModelService.ExportModel) and used on an
                        NVIDIA Jetson device.
            project (str):
                Optional. Project to run training in. Overrides project set in aiplatform.init.
            location (str):
                Optional. Location to run training in. Overrides location set in aiplatform.init.
            credentials (auth_credentials.Credentials):
                Optional. Custom credentials to use to run call training service. Overrides
                credentials set in aiplatform.init.
            labels (Dict[str, str]):
                Optional. The labels with user-defined metadata to
                organize TrainingPipelines.
                Label keys and values can be no longer than 64
                characters (Unicode codepoints), can only
                contain lowercase letters, numeric characters,
                underscores and dashes. International characters
                are allowed.
                See https://goo.gl/xmQnxf for more information
                and examples of labels.
            training_encryption_spec_key_name (Optional[str]):
                Optional. The Cloud KMS resource identifier of the customer
                managed encryption key used to protect the training pipeline. Has the
                form:
                ``projects/my-project/locations/my-region/keyRings/my-kr/cryptoKeys/my-key``.
                The key needs to be in the same region as where the compute
                resource is created.

                If set, this TrainingPipeline will be secured by this key.

                Note: Model trained by this TrainingPipeline is also secured
                by this key if ``model_to_upload`` is not set separately.

                Overrides encryption_spec_key_name set in aiplatform.init.
            model_encryption_spec_key_name (Optional[str]):
                Optional. The Cloud KMS resource identifier of the customer
                managed encryption key used to protect the model. Has the
                form:
                ``projects/my-project/locations/my-region/keyRings/my-kr/cryptoKeys/my-key``.
                The key needs to be in the same region as where the compute
                resource is created.

                If set, the trained Model will be secured by this key.

                Overrides encryption_spec_key_name set in aiplatform.init.
        Raises:
            ValueError: When an invalid prediction_type and/or model_type is provided.
        """
        if not display_name:
            display_name = self.__class__._generate_display_name()

        valid_model_types = constants.AUTOML_VIDEO_PREDICTION_MODEL_TYPES.get(
            prediction_type, None
        )

        if not valid_model_types:
            raise ValueError(
                f"'{prediction_type}' is not a supported prediction type for AutoML Video Training. "
                f"Please choose one of: {tuple(constants.AUTOML_VIDEO_PREDICTION_MODEL_TYPES.keys())}."
            )

        if model_type not in valid_model_types:
            raise ValueError(
                f"'{model_type}' is not a supported model_type for prediction_type of '{prediction_type}'. "
                f"Please choose one of: {tuple(valid_model_types)}"
            )

        super().__init__(
            display_name=display_name,
            project=project,
            location=location,
            credentials=credentials,
            labels=labels,
            training_encryption_spec_key_name=training_encryption_spec_key_name,
            model_encryption_spec_key_name=model_encryption_spec_key_name,
        )

        self._model_type = model_type
        self._prediction_type = prediction_type

    def run(
        self,
        dataset: datasets.VideoDataset,
        training_fraction_split: Optional[float] = None,
        test_fraction_split: Optional[float] = None,
        training_filter_split: Optional[str] = None,
        test_filter_split: Optional[str] = None,
        model_display_name: Optional[str] = None,
        model_labels: Optional[Dict[str, str]] = None,
        model_id: Optional[str] = None,
        parent_model: Optional[str] = None,
        is_default_version: Optional[bool] = True,
        model_version_aliases: Optional[Sequence[str]] = None,
        model_version_description: Optional[str] = None,
        sync: bool = True,
        create_request_timeout: Optional[float] = None,
    ) -> models.Model:
        """Runs the AutoML Video training job and returns a model.

        If training on a Vertex AI dataset, you can use one of the following split configurations:
            Data fraction splits:
            ``training_fraction_split``, and ``test_fraction_split`` may optionally
            be provided, they must sum to up to 1. If none of the fractions are set,
            by default roughly 80% of data will be used for training, and 20% for test.

            Data filter splits:
            Assigns input data to training, validation, and test sets
            based on the given filters, data pieces not matched by any
            filter are ignored. Currently only supported for Datasets
            containing DataItems.
            If any of the filters in this message are to match nothing, then
            they can be set as '-' (the minus sign).
            If using filter splits, all of ``training_filter_split``, ``validation_filter_split`` and
            ``test_filter_split`` must be provided.
            Supported only for unstructured Datasets.

        Args:
            dataset (datasets.VideoDataset):
                Required. The dataset within the same Project from which data will be used to train the Model. The
                Dataset must use schema compatible with Model being trained,
                and what is compatible should be described in the used
                TrainingPipeline's [training_task_definition]
                [google.cloud.aiplatform.v1beta1.TrainingPipeline.training_task_definition].
                For tabular Datasets, all their data is exported to
                training, to pick and choose from.
            training_fraction_split (float):
                Optional. The fraction of the input data that is to be used to train
                the Model. This is ignored if Dataset is not provided.
            test_fraction_split (float):
                Optional. The fraction of the input data that is to be used to evaluate
                the Model. This is ignored if Dataset is not provided.
            training_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to train the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            test_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to test the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            model_display_name (str):
                Optional. The display name of the managed Vertex AI Model. The name
                can be up to 128 characters long and can be consist of any UTF-8
                characters. If not provided upon creation, the job's display_name is used.
            model_labels (Dict[str, str]):
                Optional. The labels with user-defined metadata to
                organize your Models.
                Label keys and values can be no longer than 64
                characters (Unicode codepoints), can only
                contain lowercase letters, numeric characters,
                underscores and dashes. International characters
                are allowed.
                See https://goo.gl/xmQnxf for more information
                and examples of labels.
            model_id (str):
                Optional. The ID to use for the Model produced by this job,
                which will become the final component of the model resource name.
                This value may be up to 63 characters, and valid characters
                are `[a-z0-9_-]`. The first character cannot be a number or hyphen.
            parent_model (str):
                Optional. The resource name or model ID of an existing model.
                The new model uploaded by this job will be a version of `parent_model`.

                Only set this field when training a new version of an existing model.
            is_default_version (bool):
                Optional. When set to True, the newly uploaded model version will
                automatically have alias "default" included. Subsequent uses of
                the model produced by this job without a version specified will
                use this "default" version.

                When set to False, the "default" alias will not be moved.
                Actions targeting the model version produced by this job will need
                to specifically reference this version by ID or alias.

                New model uploads, i.e. version 1, will always be "default" aliased.
            model_version_aliases (Sequence[str]):
                Optional. User provided version aliases so that the model version
                uploaded by this job can be referenced via alias instead of
                auto-generated version ID. A default version alias will be created
                for the first version of the model.

                The format is [a-z][a-zA-Z0-9-]{0,126}[a-z0-9]
            model_version_description (str):
               Optional. The description of the model version being uploaded by this job.
            sync: bool = True
                Whether to execute this method synchronously. If False, this method
                will be executed in concurrent Future and any downstream object will
                be immediately returned and synced when the Future has completed.
            create_request_timeout (float):
                Optional. The timeout for the create request in seconds.
        Returns:
            model: The trained Vertex AI Model resource or None if training did not
                produce a Vertex AI Model.

        Raises:
            RuntimeError: If Training job has already been run or is waiting to run.
        """

        if model_display_name:
            utils.validate_display_name(model_display_name)
        if model_labels:
            utils.validate_labels(model_labels)

        if self._is_waiting_to_run():
            raise RuntimeError("AutoML Video Training is already scheduled to run.")

        if self._has_run:
            raise RuntimeError("AutoML Video Training has already run.")

        return self._run(
            dataset=dataset,
            training_fraction_split=training_fraction_split,
            test_fraction_split=test_fraction_split,
            training_filter_split=training_filter_split,
            test_filter_split=test_filter_split,
            model_display_name=model_display_name,
            model_labels=model_labels,
            model_id=model_id,
            parent_model=parent_model,
            is_default_version=is_default_version,
            model_version_aliases=model_version_aliases,
            model_version_description=model_version_description,
            sync=sync,
            create_request_timeout=create_request_timeout,
        )

    @base.optional_sync()
    def _run(
        self,
        dataset: datasets.VideoDataset,
        training_fraction_split: Optional[float] = None,
        test_fraction_split: Optional[float] = None,
        training_filter_split: Optional[str] = None,
        test_filter_split: Optional[str] = None,
        model_display_name: Optional[str] = None,
        model_labels: Optional[Dict[str, str]] = None,
        model_id: Optional[str] = None,
        parent_model: Optional[str] = None,
        is_default_version: Optional[bool] = True,
        model_version_aliases: Optional[Sequence[str]] = None,
        model_version_description: Optional[str] = None,
        sync: bool = True,
        create_request_timeout: Optional[float] = None,
    ) -> models.Model:
        """Runs the training job and returns a model.

        If training on a Vertex AI dataset, you can use one of the following split configurations:
            Data fraction splits:
            Any of ``training_fraction_split``, and ``test_fraction_split`` may optionally
            be provided, they must sum to up to 1. If none of the fractions are set,
            by default roughly 80% of data will be used for training, and 20% for test.

            Data filter splits:
            Assigns input data to training, validation, and test sets
            based on the given filters, data pieces not matched by any
            filter are ignored. Currently only supported for Datasets
            containing DataItems.
            If any of the filters in this message are to match nothing, then
            they can be set as '-' (the minus sign).
            If using filter splits, all of ``training_filter_split``, ``validation_filter_split`` and
            ``test_filter_split`` must be provided.
            Supported only for unstructured Datasets.

        Args:
            dataset (datasets.VideoDataset):
                Required. The dataset within the same Project from which data will be used to train the Model. The
                Dataset must use schema compatible with Model being trained,
                and what is compatible should be described in the used
                TrainingPipeline's [training_task_definition]
                [google.cloud.aiplatform.v1beta1.TrainingPipeline.training_task_definition].
                For tabular Datasets, all their data is exported to
                training, to pick and choose from.
            training_fraction_split (float):
                Optional. The fraction of the input data that is to be used to train
                the Model. This is ignored if Dataset is not provided.
            test_fraction_split (float):
                Optional. The fraction of the input data that is to be used to evaluate
                the Model. This is ignored if Dataset is not provided.
            training_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to train the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            test_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to test the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            model_display_name (str):
                Optional. The display name of the managed Vertex AI Model. The name
                can be up to 128 characters long and can be consist of any UTF-8
                characters. If a `base_model` was provided, the display_name in the
                base_model will be overritten with this value. If not provided upon
                creation, the job's display_name is used.
            model_labels (Dict[str, str]):
                Optional. The labels with user-defined metadata to
                organize your Models.
                Label keys and values can be no longer than 64
                characters (Unicode codepoints), can only
                contain lowercase letters, numeric characters,
                underscores and dashes. International characters
                are allowed.
                See https://goo.gl/xmQnxf for more information
                and examples of labels.
            model_id (str):
                Optional. The ID to use for the Model produced by this job,
                which will become the final component of the model resource name.
                This value may be up to 63 characters, and valid characters
                are `[a-z0-9_-]`. The first character cannot be a number or hyphen.
            parent_model (str):
                Optional. The resource name or model ID of an existing model.
                The new model uploaded by this job will be a version of `parent_model`.

                Only set this field when training a new version of an existing model.
            is_default_version (bool):
                Optional. When set to True, the newly uploaded model version will
                automatically have alias "default" included. Subsequent uses of
                the model produced by this job without a version specified will
                use this "default" version.

                When set to False, the "default" alias will not be moved.
                Actions targeting the model version produced by this job will need
                to specifically reference this version by ID or alias.

                New model uploads, i.e. version 1, will always be "default" aliased.
            model_version_aliases (Sequence[str]):
                Optional. User provided version aliases so that the model version
                uploaded by this job can be referenced via alias instead of
                auto-generated version ID. A default version alias will be created
                for the first version of the model.

                The format is [a-z][a-zA-Z0-9-]{0,126}[a-z0-9]
            model_version_description (str):
               Optional. The description of the model version being uploaded by this job.
            sync (bool):
                Whether to execute this method synchronously. If False, this method
                will be executed in concurrent Future and any downstream object will
                be immediately returned and synced when the Future has completed.
            create_request_timeout (float):
                Optional. The timeout for the create request in seconds.

        Returns:
            model: The trained Vertex AI Model resource or None if training did not
                produce a Vertex AI Model.
        """

        # Retrieve the objective-specific training task schema based on prediction_type
        training_task_definition = getattr(
            schema.training_job.definition, f"automl_video_{self._prediction_type}"
        )

        training_task_inputs_dict = {
            "modelType": self._model_type,
        }

        # gca Model to be trained
        model_tbt = gca_model.Model(encryption_spec=self._model_encryption_spec)
        model_tbt.display_name = model_display_name or self._display_name
        model_tbt.labels = model_labels or self._labels

        # AutoMLVideo does not support validation, so pass in '-' if any other filter split is provided.
        validation_filter_split = (
            "-"
            if all([training_filter_split is not None, test_filter_split is not None])
            else None
        )

        return self._run_job(
            training_task_definition=training_task_definition,
            training_task_inputs=training_task_inputs_dict,
            dataset=dataset,
            training_fraction_split=training_fraction_split,
            test_fraction_split=test_fraction_split,
            training_filter_split=training_filter_split,
            validation_filter_split=validation_filter_split,
            test_filter_split=test_filter_split,
            model=model_tbt,
            model_id=model_id,
            parent_model=parent_model,
            is_default_version=is_default_version,
            model_version_aliases=model_version_aliases,
            model_version_description=model_version_description,
            create_request_timeout=create_request_timeout,
        )

    @property
    def _model_upload_fail_string(self) -> str:
        """Helper property for model upload failure."""
        return (
            f"AutoML Video Training Pipeline {self.resource_name} is not "
            "configured to upload a Model."
        )


class AutoMLTextTrainingJob(_TrainingJob):
    _supported_training_schemas = (
        schema.training_job.definition.automl_text_classification,
        schema.training_job.definition.automl_text_extraction,
        schema.training_job.definition.automl_text_sentiment,
    )

    def __init__(
        self,
        # TODO(b/223262536): Make display_name parameter fully optional in next major release
        display_name: str,
        prediction_type: str,
        multi_label: bool = False,
        sentiment_max: int = 10,
        project: Optional[str] = None,
        location: Optional[str] = None,
        credentials: Optional[auth_credentials.Credentials] = None,
        labels: Optional[Dict[str, str]] = None,
        training_encryption_spec_key_name: Optional[str] = None,
        model_encryption_spec_key_name: Optional[str] = None,
    ):
        """Constructs a AutoML Text Training Job.

        Args:
            display_name (str):
                Required. The user-defined name of this TrainingPipeline.
            prediction_type (str):
                The type of prediction the Model is to produce, one of:
                    "classification" - A classification model analyzes text data and
                        returns a list of categories that apply to the text found in the data.
                        Vertex AI offers both single-label and multi-label text classification models.
                    "extraction" - An entity extraction model inspects text data
                        for known entities referenced in the data and
                        labels those entities in the text.
                    "sentiment" - A sentiment analysis model inspects text data and identifies the
                        prevailing emotional opinion within it, especially to determine a writer's attitude
                        as positive, negative, or neutral.
            multi_label (bool):
                Required and only applicable for text classification task. If false, a single-label (multi-class) Model will be trained (i.e.
                assuming that for each text snippet just up to one annotation may be
                applicable). If true, a multi-label Model will be trained (i.e.
                assuming that for each text snippet multiple annotations may be
                applicable).
            sentiment_max (int):
                Required and only applicable for sentiment task. A sentiment is expressed as an integer
                ordinal, where higher value means a more
                positive sentiment. The range of sentiments that
                will be used is between 0 and sentimentMax
                (inclusive on both ends), and all the values in
                the range must be represented in the dataset
                before a model can be created.
                Only the Annotations with this sentimentMax will
                be used for training. sentimentMax value must be
                between 1 and 10 (inclusive).
            project (str):
                Optional. Project to run training in. Overrides project set in aiplatform.init.
            location (str):
                Optional. Location to run training in. Overrides location set in aiplatform.init.
            credentials (auth_credentials.Credentials):
                Optional. Custom credentials to use to run call training service. Overrides
                credentials set in aiplatform.init.
            labels (Dict[str, str]):
                Optional. The labels with user-defined metadata to
                organize TrainingPipelines.
                Label keys and values can be no longer than 64
                characters (Unicode codepoints), can only
                contain lowercase letters, numeric characters,
                underscores and dashes. International characters
                are allowed.
                See https://goo.gl/xmQnxf for more information
                and examples of labels.
            training_encryption_spec_key_name (Optional[str]):
                Optional. The Cloud KMS resource identifier of the customer
                managed encryption key used to protect the training pipeline. Has the
                form:
                ``projects/my-project/locations/my-region/keyRings/my-kr/cryptoKeys/my-key``.
                The key needs to be in the same region as where the compute
                resource is created.

                If set, this TrainingPipeline will be secured by this key.

                Note: Model trained by this TrainingPipeline is also secured
                by this key if ``model_to_upload`` is not set separately.

                Overrides encryption_spec_key_name set in aiplatform.init.
            model_encryption_spec_key_name (Optional[str]):
                Optional. The Cloud KMS resource identifier of the customer
                managed encryption key used to protect the model. Has the
                form:
                ``projects/my-project/locations/my-region/keyRings/my-kr/cryptoKeys/my-key``.
                The key needs to be in the same region as where the compute
                resource is created.

                If set, the trained Model will be secured by this key.

                Overrides encryption_spec_key_name set in aiplatform.init.
        """
        if not display_name:
            display_name = self.__class__._generate_display_name()
        super().__init__(
            display_name=display_name,
            project=project,
            location=location,
            credentials=credentials,
            labels=labels,
            training_encryption_spec_key_name=training_encryption_spec_key_name,
            model_encryption_spec_key_name=model_encryption_spec_key_name,
        )

        training_task_definition: str
        training_task_inputs_dict: proto.Message

        if prediction_type == "classification":
            training_task_definition = (
                schema.training_job.definition.automl_text_classification
            )

            training_task_inputs_dict = (
                training_job_inputs.AutoMlTextClassificationInputs(
                    multi_label=multi_label
                )
            )
        elif prediction_type == "extraction":
            training_task_definition = (
                schema.training_job.definition.automl_text_extraction
            )

            training_task_inputs_dict = training_job_inputs.AutoMlTextExtractionInputs()
        elif prediction_type == "sentiment":
            training_task_definition = (
                schema.training_job.definition.automl_text_sentiment
            )

            training_task_inputs_dict = training_job_inputs.AutoMlTextSentimentInputs(
                sentiment_max=sentiment_max
            )
        else:
            raise ValueError(
                "Prediction type must be one of 'classification', 'extraction', or 'sentiment'."
            )

        self._training_task_definition = training_task_definition
        self._training_task_inputs_dict = training_task_inputs_dict

    def run(
        self,
        dataset: datasets.TextDataset,
        training_fraction_split: Optional[float] = None,
        validation_fraction_split: Optional[float] = None,
        test_fraction_split: Optional[float] = None,
        training_filter_split: Optional[str] = None,
        validation_filter_split: Optional[str] = None,
        test_filter_split: Optional[str] = None,
        model_display_name: Optional[str] = None,
        model_labels: Optional[Dict[str, str]] = None,
        model_id: Optional[str] = None,
        parent_model: Optional[str] = None,
        is_default_version: Optional[bool] = True,
        model_version_aliases: Optional[Sequence[str]] = None,
        model_version_description: Optional[str] = None,
        sync: bool = True,
        create_request_timeout: Optional[float] = None,
    ) -> models.Model:
        """Runs the training job and returns a model.

        If training on a Vertex AI dataset, you can use one of the following split configurations:
            Data fraction splits:
            Any of ``training_fraction_split``, ``validation_fraction_split`` and
            ``test_fraction_split`` may optionally be provided, they must sum to up to 1. If
            the provided ones sum to less than 1, the remainder is assigned to sets as
            decided by Vertex AI. If none of the fractions are set, by default roughly 80%
            of data will be used for training, 10% for validation, and 10% for test.

            Data filter splits:
            Assigns input data to training, validation, and test sets
            based on the given filters, data pieces not matched by any
            filter are ignored. Currently only supported for Datasets
            containing DataItems.
            If any of the filters in this message are to match nothing, then
            they can be set as '-' (the minus sign).
            If using filter splits, all of ``training_filter_split``, ``validation_filter_split`` and
            ``test_filter_split`` must be provided.
            Supported only for unstructured Datasets.

        Args:
            dataset (datasets.TextDataset):
                Required. The dataset within the same Project from which data will be used to train the Model. The
                Dataset must use schema compatible with Model being trained,
                and what is compatible should be described in the used
                TrainingPipeline's [training_task_definition]
                [google.cloud.aiplatform.v1beta1.TrainingPipeline.training_task_definition].
            training_fraction_split (float):
                Optional. The fraction of the input data that is to be used to train
                the Model. This is ignored if Dataset is not provided.
            validation_fraction_split (float):
                Optional. The fraction of the input data that is to be used to validate
                the Model. This is ignored if Dataset is not provided.
            test_fraction_split (float):
                Optional. The fraction of the input data that is to be used to evaluate
                the Model. This is ignored if Dataset is not provided.
            training_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to train the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            validation_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to validate the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            test_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to test the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            model_display_name (str):
                Optional. The display name of the managed Vertex AI Model.
                The name can be up to 128 characters long and can consist
                of any UTF-8 characters.

                If not provided upon creation, the job's display_name is used.
            model_labels (Dict[str, str]):
                Optional. The labels with user-defined metadata to
                organize your Models.
                Label keys and values can be no longer than 64
                characters (Unicode codepoints), can only
                contain lowercase letters, numeric characters,
                underscores and dashes. International characters
                are allowed.
                See https://goo.gl/xmQnxf for more information
                and examples of labels..
            model_id (str):
                Optional. The ID to use for the Model produced by this job,
                which will become the final component of the model resource name.
                This value may be up to 63 characters, and valid characters
                are `[a-z0-9_-]`. The first character cannot be a number or hyphen.
            parent_model (str):
                Optional. The resource name or model ID of an existing model.
                The new model uploaded by this job will be a version of `parent_model`.

                Only set this field when training a new version of an existing model.
            is_default_version (bool):
                Optional. When set to True, the newly uploaded model version will
                automatically have alias "default" included. Subsequent uses of
                the model produced by this job without a version specified will
                use this "default" version.

                When set to False, the "default" alias will not be moved.
                Actions targeting the model version produced by this job will need
                to specifically reference this version by ID or alias.

                New model uploads, i.e. version 1, will always be "default" aliased.
            model_version_aliases (Sequence[str]):
                Optional. User provided version aliases so that the model version
                uploaded by this job can be referenced via alias instead of
                auto-generated version ID. A default version alias will be created
                for the first version of the model.

                The format is [a-z][a-zA-Z0-9-]{0,126}[a-z0-9]
            model_version_description (str):
               Optional. The description of the model version being uploaded by this job.
            sync (bool):
                Whether to execute this method synchronously. If False, this method
                will be executed in concurrent Future and any downstream object will
                be immediately returned and synced when the Future has completed.
            create_request_timeout (float):
                Optional. The timeout for the create request in seconds
        Returns:
            model: The trained Vertex AI Model resource.

        Raises:
            RuntimeError: If Training job has already been run or is waiting to run.
        """

        if model_display_name:
            utils.validate_display_name(model_display_name)
        if model_labels:
            utils.validate_labels(model_labels)

        if self._is_waiting_to_run():
            raise RuntimeError("AutoML Text Training is already scheduled to run.")

        if self._has_run:
            raise RuntimeError("AutoML Text Training has already run.")

        return self._run(
            dataset=dataset,
            training_fraction_split=training_fraction_split,
            validation_fraction_split=validation_fraction_split,
            test_fraction_split=test_fraction_split,
            training_filter_split=training_filter_split,
            validation_filter_split=validation_filter_split,
            test_filter_split=test_filter_split,
            model_display_name=model_display_name,
            model_labels=model_labels,
            model_id=model_id,
            parent_model=parent_model,
            is_default_version=is_default_version,
            model_version_aliases=model_version_aliases,
            model_version_description=model_version_description,
            sync=sync,
            create_request_timeout=create_request_timeout,
        )

    @base.optional_sync()
    def _run(
        self,
        dataset: datasets.TextDataset,
        training_fraction_split: Optional[float] = None,
        validation_fraction_split: Optional[float] = None,
        test_fraction_split: Optional[float] = None,
        training_filter_split: Optional[str] = None,
        validation_filter_split: Optional[str] = None,
        test_filter_split: Optional[str] = None,
        model_display_name: Optional[str] = None,
        model_labels: Optional[Dict[str, str]] = None,
        model_id: Optional[str] = None,
        parent_model: Optional[str] = None,
        is_default_version: Optional[bool] = True,
        model_version_aliases: Optional[Sequence[str]] = None,
        model_version_description: Optional[str] = None,
        sync: bool = True,
        create_request_timeout: Optional[float] = None,
    ) -> models.Model:
        """Runs the training job and returns a model.

        If training on a Vertex AI dataset, you can use one of the following split configurations:
            Data fraction splits:
            Any of ``training_fraction_split``, ``validation_fraction_split`` and
            ``test_fraction_split`` may optionally be provided, they must sum to up to 1. If
            the provided ones sum to less than 1, the remainder is assigned to sets as
            decided by Vertex AI. If none of the fractions are set, by default roughly 80%
            of data will be used for training, 10% for validation, and 10% for test.

            Data filter splits:
            Assigns input data to training, validation, and test sets
            based on the given filters, data pieces not matched by any
            filter are ignored. Currently only supported for Datasets
            containing DataItems.
            If any of the filters in this message are to match nothing, then
            they can be set as '-' (the minus sign).
            If using filter splits, all of ``training_filter_split``, ``validation_filter_split`` and
            ``test_filter_split`` must be provided.
            Supported only for unstructured Datasets.

        Args:
            dataset (datasets.TextDataset):
                Required. The dataset within the same Project from which data will be used to train the Model. The
                Dataset must use schema compatible with Model being trained,
                and what is compatible should be described in the used
                TrainingPipeline's [training_task_definition]
                [google.cloud.aiplatform.v1beta1.TrainingPipeline.training_task_definition].
                For Text Datasets, all their data is exported to
                training, to pick and choose from.
            training_fraction_split (float):
                Optional. The fraction of the input data that is to be used to train
                the Model. This is ignored if Dataset is not provided.
            validation_fraction_split (float):
                Optional. The fraction of the input data that is to be used to validate
                the Model. This is ignored if Dataset is not provided.
            test_fraction_split (float):
                Optional. The fraction of the input data that is to be used to evaluate
                the Model. This is ignored if Dataset is not provided.
            training_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to train the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            validation_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to validate the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            test_filter_split (str):
                Optional. A filter on DataItems of the Dataset. DataItems that match
                this filter are used to test the Model. A filter with same syntax
                as the one used in DatasetService.ListDataItems may be used. If a
                single DataItem is matched by more than one of the FilterSplit filters,
                then it is assigned to the first set that applies to it in the training,
                validation, test order. This is ignored if Dataset is not provided.
            model_display_name (str):
                Optional. If the script produces a managed Vertex AI Model. The display name of
                the Model. The name can be up to 128 characters long and can be consist
                of any UTF-8 characters.

                If not provided upon creation, the job's display_name is used.
            model_labels (Dict[str, str]):
                Optional. The labels with user-defined metadata to
                organize your Models.
                Label keys and values can be no longer than 64
                characters (Unicode codepoints), can only
                contain lowercase letters, numeric characters,
                underscores and dashes. International characters
                are allowed.
                See https://goo.gl/xmQnxf for more information
                and examples of labels.
            model_id (str):
                Optional. The ID to use for the Model produced by this job,
                which will become the final component of the model resource name.
                This value may be up to 63 characters, and valid characters
                are `[a-z0-9_-]`. The first character cannot be a number or hyphen.
            parent_model (str):
                Optional. The resource name or model ID of an existing model.
                The new model uploaded by this job will be a version of `parent_model`.

                Only set this field when training a new version of an existing model.
            is_default_version (bool):
                Optional. When set to True, the newly uploaded model version will
                automatically have alias "default" included. Subsequent uses of
                the model produced by this job without a version specified will
                use this "default" version.

                When set to False, the "default" alias will not be moved.
                Actions targeting the model version produced by this job will need
                to specifically reference this version by ID or alias.

                New model uploads, i.e. version 1, will always be "default" aliased.
            model_version_aliases (Sequence[str]):
                Optional. User provided version aliases so that the model version
                uploaded by this job can be referenced via alias instead of
                auto-generated version ID. A default version alias will be created
                for the first version of the model.

                The format is [a-z][a-zA-Z0-9-]{0,126}[a-z0-9]
            model_version_description (str):
               Optional. The description of the model version being uploaded by this job.
            sync (bool):
                Whether to execute this method synchronously. If False, this method
                will be executed in concurrent Future and any downstream object will
                be immediately returned and synced when the Future has completed.
            create_request_timeout (float):
                Optional. The timeout for the create request in seconds.

        Returns:
            model: The trained Vertex AI Model resource or None if training did not
                produce a Vertex AI Model.
        """

        model = gca_model.Model(
            display_name=model_display_name or self._display_name,
            labels=model_labels or self._labels,
            encryption_spec=self._model_encryption_spec,
        )

        return self._run_job(
            training_task_definition=self._training_task_definition,
            training_task_inputs=self._training_task_inputs_dict,
            dataset=dataset,
            training_fraction_split=training_fraction_split,
            validation_fraction_split=validation_fraction_split,
            test_fraction_split=test_fraction_split,
            training_filter_split=training_filter_split,
            validation_filter_split=validation_filter_split,
            test_filter_split=test_filter_split,
            model=model,
            model_id=model_id,
            parent_model=parent_model,
            is_default_version=is_default_version,
            model_version_aliases=model_version_aliases,
            model_version_description=model_version_description,
            create_request_timeout=create_request_timeout,
        )

    @property
    def _model_upload_fail_string(self) -> str:
        """Helper property for model upload failure."""
        return (
            f"AutoML Text Training Pipeline {self.resource_name} is not "
            "configured to upload a Model."
        )
