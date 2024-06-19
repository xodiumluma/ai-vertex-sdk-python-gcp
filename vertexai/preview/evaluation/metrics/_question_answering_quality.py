# -*- coding: utf-8 -*-

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

from typing import Optional
from vertexai.preview.evaluation import constants
from vertexai.preview.evaluation.metrics import _base


class QuestionAnsweringQuality(_base._ModelBasedMetric):
    """The model-based pointwise metric for Question Answering Quality."""

    _metric_name = constants.Metric.QUESTION_ANSWERING_QUALITY

    def __init__(self, *, use_reference: bool = False, version: Optional[int] = None):
        super().__init__(
            metric=QuestionAnsweringQuality._metric_name,
            use_reference=use_reference,
            version=version,
        )
