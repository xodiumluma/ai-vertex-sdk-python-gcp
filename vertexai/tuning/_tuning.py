# Copyright 2024 Google LLC
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
# pylint: disable=protected-access
"""Classes to support Tuning."""

from typing import Dict, List, Optional, Union

from google.auth import credentials as auth_credentials

from google.cloud import aiplatform
from google.cloud.aiplatform import base as aiplatform_base
from google.cloud.aiplatform import compat
from google.cloud.aiplatform import initializer as aiplatform_initializer
from google.cloud.aiplatform import jobs
from google.cloud.aiplatform import utils as aiplatform_utils
from google.cloud.aiplatform.utils import _ipython_utils
from google.cloud.aiplatform_v1.services import (
    gen_ai_tuning_service as gen_ai_tuning_service_v1,
)
from google.cloud.aiplatform_v1beta1.services import (
    gen_ai_tuning_service as gen_ai_tuning_service_v1beta1,
)
from google.cloud.aiplatform_v1beta1.types import (
    tuning_job as gca_tuning_job_types,
)
from google.cloud.aiplatform_v1beta1 import types as gca_types

from google.rpc import status_pb2  # type: ignore


_LOGGER = aiplatform_base.Logger(__name__)


class TuningJobClientWithOverride(aiplatform_utils.ClientWithOverride):
    _is_temporary = True
    _default_version = compat.V1BETA1
    _version_map = (
        (compat.V1, gen_ai_tuning_service_v1.client.GenAiTuningServiceClient),
        (compat.V1BETA1, gen_ai_tuning_service_v1beta1.client.GenAiTuningServiceClient),
    )


class TuningJob(aiplatform_base._VertexAiResourceNounPlus):
    """Represents a TuningJob that runs with Google owned models."""

    _resource_noun = "tuningJobs"
    _getter_method = "get_tuning_job"
    _list_method = "list_tuning_jobs"
    _cancel_method = "cancel_tuning_job"
    _delete_method = "delete_tuning_job"
    _parse_resource_name_method = "parse_tuning_job_path"
    _format_resource_name_method = "tuning_job_path"
    _job_type = "tuning/tuningJob"
    _has_displayed_experiments_button = False

    client_class = TuningJobClientWithOverride

    _gca_resource: gca_tuning_job_types.TuningJob
    api_client: gen_ai_tuning_service_v1beta1.client.GenAiTuningServiceClient

    def __init__(self, tuning_job_name: str):
        super().__init__(resource_name=tuning_job_name)
        self._gca_resource: gca_tuning_job_types.TuningJob = self._get_gca_resource(
            resource_name=tuning_job_name
        )

    def refresh(self) -> "TuningJob":
        """Refreshed the tuning job from the service."""
        self._gca_resource: gca_tuning_job_types.TuningJob = self._get_gca_resource(
            resource_name=self.resource_name
        )
        if self.experiment and not self._has_displayed_experiments_button:
            self._has_displayed_experiments_button = True
            _ipython_utils.display_experiment_button(self.experiment)
        return self

    @property
    def tuned_model_name(self) -> Optional[str]:
        return self._gca_resource.tuned_model.model

    @property
    def tuned_model_endpoint_name(self) -> Optional[str]:
        return self._gca_resource.tuned_model.endpoint

    @property
    def _experiment_name(self) -> Optional[str]:
        return self._gca_resource.experiment

    @property
    def experiment(self) -> Optional[aiplatform.Experiment]:
        if self._experiment_name:
            return aiplatform.Experiment(experiment_name=self._experiment_name)

    @property
    def state(self) -> gca_types.JobState:
        return self._gca_resource.state

    @property
    def has_ended(self):
        return self.state in jobs._JOB_COMPLETE_STATES

    @property
    def has_succeeded(self):
        return self.state == gca_types.JobState.JOB_STATE_SUCCEEDED

    @property
    def error(self) -> Optional[status_pb2.Status]:
        return self._gca_resource.error

    @property
    def tuning_data_statistics(self) -> gca_tuning_job_types.TuningDataStats:
        return self._gca_resource.tuning_data_stats

    @classmethod
    def _create(
        cls,
        *,
        base_model: str,
        tuning_spec: Union[
            gca_tuning_job_types.SupervisedTuningSpec,
            gca_tuning_job_types.DistillationSpec,
        ],
        tuned_model_display_name: Optional[str] = None,
        description: Optional[str] = None,
        labels: Optional[Dict[str, str]] = None,
        project: Optional[str] = None,
        location: Optional[str] = None,
        credentials: Optional[auth_credentials.Credentials] = None,
    ) -> "TuningJob":
        r"""Submits TuningJob.

        Args:
            base_model (str):
                Model name for tuning, e.g., "gemini-1.0-pro"
                or "gemini-1.0-pro-001".

                This field is a member of `oneof`_ ``source_model``.
            tuning_spec: Tuning Spec for Fine Tuning.
                Supported types: SupervisedTuningSpec, DistillationSpec.
            tuned_model_display_name: The display name of the
                [TunedModel][google.cloud.aiplatform.v1.Model]. The name can
                be up to 128 characters long and can consist of any UTF-8
                characters.
            description: The description of the `TuningJob`.
            labels: The labels with user-defined metadata to organize
                [TuningJob][google.cloud.aiplatform.v1.TuningJob] and
                generated resources such as
                [Model][google.cloud.aiplatform.v1.Model] and
                [Endpoint][google.cloud.aiplatform.v1.Endpoint].

                Label keys and values can be no longer than 64 characters
                (Unicode codepoints), can only contain lowercase letters,
                numeric characters, underscores and dashes. International
                characters are allowed.

                See https://goo.gl/xmQnxf for more information and examples
                of labels.
            project: Project to run the tuning job in.
                Overrides project set in aiplatform.init.
            location: Location to run the tuning job in.
                Overrides location set in aiplatform.init.
            credentials: Custom credentials to use to call tuning job service.
                Overrides credentials set in aiplatform.init.

        Returns:
            Submitted TuningJob.

        Raises:
            RuntimeError is tuning_spec kind is unsupported
        """
        _LOGGER.log_create_with_lro(cls)

        if not tuned_model_display_name:
            tuned_model_display_name = cls._generate_display_name()

        gca_tuning_job = gca_tuning_job_types.TuningJob(
            base_model=base_model,
            tuned_model_display_name=tuned_model_display_name,
            description=description,
            labels=labels,
            # The tuning_spec one_of is set later
        )

        if isinstance(tuning_spec, gca_tuning_job_types.SupervisedTuningSpec):
            gca_tuning_job.supervised_tuning_spec = tuning_spec
        elif isinstance(tuning_spec, gca_tuning_job_types.DistillationSpec):
            gca_tuning_job.distillation_spec = tuning_spec
        else:
            raise RuntimeError(f"Unsupported tuning_spec kind: {tuning_spec}")

        if aiplatform_initializer.global_config.encryption_spec_key_name:
            gca_tuning_job.encryption_spec.kms_key_name = (
                aiplatform_initializer.global_config.encryption_spec_key_name
            )

        tuning_job: TuningJob = cls._construct_sdk_resource_from_gapic(
            gapic_resource=gca_tuning_job,
            project=project,
            location=location,
            credentials=credentials,
        )

        parent = aiplatform_initializer.global_config.common_location_path(
            project=project, location=location
        )

        created_gca_tuning_job = tuning_job.api_client.create_tuning_job(
            parent=parent,
            tuning_job=gca_tuning_job,
        )
        tuning_job._gca_resource = created_gca_tuning_job

        _LOGGER.log_create_complete(
            cls=cls,
            resource=created_gca_tuning_job,
            variable_name="tuning_job",
            module_name="sft",
        )
        _LOGGER.info(f"View Tuning Job:\n{tuning_job._dashboard_url()}")
        if tuning_job._experiment_name:
            _LOGGER.info(f"View experiment:\n{tuning_job._experiment.dashboard_url}")

        return tuning_job

    def cancel(self):
        self.api_client.cancel_tuning_job(name=self.resource_name)

    @classmethod
    def list(cls, filter: Optional[str] = None) -> List["TuningJob"]:
        """Lists TuningJobs.

        Args:
            filter: The standard list filter.

        Returns:
            A list of TuningJob objects.
        """
        return cls._list(filter=filter)

    def _dashboard_url(self) -> str:
        """Returns the Google Cloud console URL where job can be viewed."""
        fields = self._parse_resource_name(self.resource_name)
        location = fields.pop("location")
        project = fields.pop("project")
        job = list(fields.values())[0]
        url = f"https://console.cloud.google.com/vertex-ai/generative/language/locations/{location}/tuning/tuningJob/{job}?project={project}"
        return url
