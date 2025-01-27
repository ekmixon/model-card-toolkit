# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Utilities for reading metadata from MLMD instances in TFX-OSS pipelines."""

import enum
import os
from typing import Iterable, List, Optional, Text, Union

from absl import logging
import attr
from model_card_toolkit import model_card as model_card_module
import tensorflow as tf
import tensorflow_model_analysis as tfma

import ml_metadata as mlmd
from ml_metadata.proto import metadata_store_pb2
from tensorflow_metadata.proto.v0 import statistics_pb2

# A list of artifact type names used by TFX 0.21 and later versions.
_TFX_DATASET_TYPE = 'Examples'
_TFX_STATS_TYPE = 'ExampleStatistics'
_TFX_MODEL_TYPE = 'Model'
_TFX_METRICS_TYPE = 'ModelEvaluation'
_TFX_TRAINER_TYPE = 'tfx.components.trainer.component.Trainer'


@attr.s(auto_attribs=True)
class PipelineTypes(object):
  """A registry of required MLMD types about a TFX pipeline."""
  # a list of required artifact types
  dataset_type: metadata_store_pb2.ArtifactType
  stats_type: metadata_store_pb2.ArtifactType
  model_type: metadata_store_pb2.ArtifactType
  metrics_type: metadata_store_pb2.ArtifactType
  # a list of required execution types
  trainer_type: metadata_store_pb2.ExecutionType


def _get_tfx_pipeline_types(
    store: mlmd.MetadataStore) -> PipelineTypes:
  """Retrieves the registered types in the given `store`.

  Args:
    store: A ml-metadata MetadataStore to retrieve ArtifactTypes from.

  Returns:
    A instance of PipelineTypes containing store pipeline types.

  Raises:
    ValueError: If the `store` does not have MCT related types and is not
      considered a valid TFX store.
  """
  artifact_types = {atype.name: atype for atype in store.get_artifact_types()}
  expected_artifact_types = {
      _TFX_DATASET_TYPE, _TFX_STATS_TYPE, _TFX_MODEL_TYPE, _TFX_METRICS_TYPE
  }
  missing_types = expected_artifact_types.difference(artifact_types.keys())
  if missing_types:
    raise ValueError(
        f'Given `store` is invalid: missing ArtifactTypes: {missing_types}.'
    )
  execution_types = {etype.name: etype for etype in store.get_execution_types()}
  expected_execution_types = {_TFX_TRAINER_TYPE}
  missing_types = expected_execution_types.difference(execution_types.keys())
  if missing_types:
    raise ValueError(
        f'Given `store` is invalid: missing ExecutionTypes: {missing_types}.')
  return PipelineTypes(
      dataset_type=artifact_types[_TFX_DATASET_TYPE],
      stats_type=artifact_types[_TFX_STATS_TYPE],
      model_type=artifact_types[_TFX_MODEL_TYPE],
      metrics_type=artifact_types[_TFX_METRICS_TYPE],
      trainer_type=execution_types[_TFX_TRAINER_TYPE])


def _validate_model_id(store: mlmd.MetadataStore,
                       model_type: metadata_store_pb2.ArtifactType,
                       model_id: int) -> metadata_store_pb2.Artifact:
  """Validates the given `model_id` against the `store`.

  Args:
    store: A ml-metadata MetadataStore to be validated.
    model_type: The Model ArtifactType in the `store`.
    model_id: The id for the model artifact in the `store`.

  Returns:
    The model artifact with the id.

  Raises:
    ValueError: If the `model_id` cannot be resolved as a Model artifact in the
      given `store`.
  """
  model_artifacts = store.get_artifacts_by_id([model_id])
  if not model_artifacts:
    raise ValueError(f'Input model_id cannot be found: {model_id}.')
  model = model_artifacts[0]
  if model.type_id != model_type.id:
    raise ValueError(
        f'Found artifact with `model_id` is not an instance of Model: {model}.')
  return model


@enum.unique
class _Direction(enum.Enum):
  """An enum of directions when traversing MLMD lineage."""
  ANCESTOR = 1
  SUCCESSOR = 2


def _get_one_hop_artifacts(
    store: mlmd.MetadataStore,
    artifact_ids: Iterable[int],
    direction: _Direction,
    filter_type: Optional[metadata_store_pb2.ArtifactType] = None
) -> List[metadata_store_pb2.Artifact]:
  """Gets a list of artifacts within 1-hop neighborhood of the `artifact_ids`.

  Args:
    store: A ml-metadata MetadataStore to look for neighborhood artifacts.
    artifact_ids: The artifacts' ids in the `store`.
    direction: A direction to specify whether returning ancestors or successors.
    filter_type: An optional type filter of the returned artifacts, if given
      then only artifacts of that type is returned.

  Returns:
    A list of qualified artifacts within 1-hop neighborhood in the `store`.
  """
  traverse_events = {}
  if direction == _Direction.ANCESTOR:
    traverse_events['execution'] = (metadata_store_pb2.Event.OUTPUT,
                                    metadata_store_pb2.Event.DECLARED_OUTPUT)
    traverse_events['artifact'] = (metadata_store_pb2.Event.INPUT,
                                   metadata_store_pb2.Event.DECLARED_INPUT)
  elif direction == _Direction.SUCCESSOR:
    traverse_events['execution'] = (metadata_store_pb2.Event.INPUT,
                                    metadata_store_pb2.Event.DECLARED_INPUT)
    traverse_events['artifact'] = (metadata_store_pb2.Event.OUTPUT,
                                   metadata_store_pb2.Event.DECLARED_OUTPUT)
  executions_ids = set(
      event.execution_id
      for event in store.get_events_by_artifact_ids(artifact_ids)
      if event.type in traverse_events['execution'])
  artifacts_ids = set(
      event.artifact_id
      for event in store.get_events_by_execution_ids(executions_ids)
      if event.type in traverse_events['artifact'])
  return [
      artifact for artifact in store.get_artifacts_by_id(artifacts_ids)
      if not filter_type or artifact.type_id == filter_type.id
  ]


def _get_one_hop_executions(
    store: mlmd.MetadataStore,
    artifact_ids: Iterable[int],
    direction: _Direction,
    filter_type: Optional[metadata_store_pb2.ExecutionType] = None
) -> List[metadata_store_pb2.Execution]:
  """Gets a list of executions within 1-hop neighborhood of the `artifact_ids`.

  Args:
    store: A ml-metadata MetadataStore to look for neighborhood executions.
    artifact_ids: The artifacts' ids in the `store`.
    direction: A direction to specify whether returning ancestors or successors.
    filter_type: An optional type filter of the returned executions, if given
      then only executions of that type is returned.

  Returns:
    A list of qualified executions within 1-hop neighborhood in the `store`.
  """
  if direction == _Direction.ANCESTOR:
    traverse_event = (metadata_store_pb2.Event.OUTPUT,
                      metadata_store_pb2.Event.DECLARED_OUTPUT)
  elif direction == _Direction.SUCCESSOR:
    traverse_event = (metadata_store_pb2.Event.INPUT,
                      metadata_store_pb2.Event.DECLARED_INPUT)
  executions_ids = set(
      event.execution_id
      for event in store.get_events_by_artifact_ids(artifact_ids)
      if event.type in traverse_event)
  return [
      execution for execution in store.get_executions_by_id(executions_ids)
      if not filter_type or execution.type_id == filter_type.id
  ]


def get_metrics_artifacts_for_model(
    store: mlmd.MetadataStore,
    model_id: int,
    pipeline_types: Optional[PipelineTypes] = None
) -> List[metadata_store_pb2.Artifact]:
  """Gets a list of evaluation artifacts from a model artifact.

  It looks for the evaluator component runs that take the given model as input.
  Then it returns the metrics artifact of that component run.

  Args:
    store: A ml-metadata MetadataStore to look for evaluation metrics.
    model_id: The id for the model artifact in the `store`.
    pipeline_types: An optional set of types if the `store` uses custom types.

  Returns:
    A list of metrics artifacts produced by the Evaluator component runs
    which take the given model artifact as the input.

  Raises:
    ValueError: If the `model_id` cannot be resolved as a model artifact in the
      given `store`.
  """
  if not pipeline_types:
    pipeline_types = _get_tfx_pipeline_types(store)
  _validate_model_id(store, pipeline_types.model_type, model_id)
  return _get_one_hop_artifacts(store, [model_id], _Direction.SUCCESSOR,
                                pipeline_types.metrics_type)


def get_stats_artifacts_for_model(
    store: mlmd.MetadataStore,
    model_id: int,
    pipeline_types: Optional[PipelineTypes] = None
) -> List[metadata_store_pb2.Artifact]:
  """Gets a list of statistics artifacts from a model artifact.

  It first looks for the input datasets of the trainer that produces the model.
  If the model uses a transformed examples, it further looks for the original
  dataset. Then it returns the statistics artifact of the found dataset(s).

  Args:
    store: A ml-metadata MetadataStore instance.
    model_id: The id for the model artifact in the `store`.
    pipeline_types: An optional set of types if the `store` uses custom types.

  Returns:
    A list of statistics artifacts produced by the StatsGen component runs
    for the datasets which are used to train the model.

  Raises:
    ValueError: If the `model_id` cannot be resolved as a model artifact in the
      given `store`.
  """
  if not pipeline_types:
    pipeline_types = _get_tfx_pipeline_types(store)
  _validate_model_id(store, pipeline_types.model_type, model_id)
  trainer_examples = _get_one_hop_artifacts(store, [model_id],
                                            _Direction.ANCESTOR,
                                            pipeline_types.dataset_type)
  # If trainer takes transformed example, we look for its original dataset.
  dataset_ids = set()
  transformed_example_ids = set()
  for example in trainer_examples:
    if example.uri.find('/Transform/') != -1:
      transformed_example_ids.add(example.id)
    else:
      dataset_ids.add(example.id)
  dataset_ids.update(
      dataset.id for dataset in _get_one_hop_artifacts(
          store, transformed_example_ids, _Direction.ANCESTOR,
          pipeline_types.dataset_type))
  return _get_one_hop_artifacts(store, dataset_ids, _Direction.SUCCESSOR,
                                pipeline_types.stats_type)


def _property_value(
    node: Union[metadata_store_pb2.Artifact, metadata_store_pb2.Execution,
                metadata_store_pb2.Context],
    name: Text,
    is_custom_property: bool = False) -> Optional[Union[int, float, Text]]:
  """Given a MLMD node and a (custom) property name, returns its value if any.

  Args:
    node: A node in MLMD lineage graph. It is one of MLMD Artifact, Execution,
      or Context.
    name: The key of the properties or custom properties.
    is_custom_property: Indicates whether the name is a custom property.

  Returns:
    The value of the property if found in the node; If not, returns None.
  """
  properties = node.custom_properties if is_custom_property else node.properties
  if name not in properties:
    return None
  if properties[name].WhichOneof('value') == 'int_value':
    return properties[name].int_value
  if properties[name].WhichOneof('value') == 'float_value':
    return properties[name].double_value
  return properties[name].string_value


def generate_model_card_for_model(
    store: mlmd.MetadataStore,
    model_id: int,
    pipeline_types: Optional[PipelineTypes] = None
) -> model_card_module.ModelCard:
  """Populates model card properties for a model artifact.

  It traverse the parents and children of the model artifact, and maps related
  artifact properties and lineage information to model card property. The
  graphics derived from the artifact payload are handled separately.

  Args:
    store: A ml-metadata MetadataStore instance.
    model_id: The id for the model artifact in the `store`.
    pipeline_types: An optional set of types if the `store` uses custom types.

  Returns:
    A ModelCard data object with the properties.

  Raises:
    ValueError: If the `model_id` cannot be resolved as a model artifact in the
      given `store`.
  """
  if not pipeline_types:
    pipeline_types = _get_tfx_pipeline_types(store)
  _validate_model_id(store, pipeline_types.model_type, model_id)
  model_card = model_card_module.ModelCard()
  model_details = model_card.model_details
  trainers = _get_one_hop_executions(store, [model_id], _Direction.ANCESTOR,
                                     pipeline_types.trainer_type)
  if trainers:
    model_details.name = _property_value(trainers[-1], 'module_file')
    model_details.version.name = _property_value(trainers[0], 'checksum_md5')
    model_details.references = [
        model_card_module.Reference(
            reference=_property_value(trainers[0], 'pipeline_name'))
    ]
  return model_card


def read_stats_proto(
    stats_artifact_uri: Text,
    split: Text) -> Optional[statistics_pb2.DatasetFeatureStatisticsList]:
  """Reads DatasetFeatureStatisticsList proto from provided stats artifact uri.

  Args:
    stats_artifact_uri: the output artifact path of a StatsGen component.
    split: the data split to fetch stats from.

  Returns:
    If the artifact uri does not exist, returns None. Otherwise, returns the
    eval split stats as DatasetFeatureStatisticsList.
  """
  stats = statistics_pb2.DatasetFeatureStatisticsList()
  feature_stats_path = os.path.join(stats_artifact_uri, split,
                                    'FeatureStats.pb')
  stats_tfrecord_path = os.path.join(stats_artifact_uri,
                                     split, 'stats_tfrecord')

  if os.path.exists(feature_stats_path):
    with tf.io.gfile.GFile(feature_stats_path, mode='rb') as f:
      stats.ParseFromString(f.read())
    return stats
  elif os.path.exists(stats_tfrecord_path):
    serialized_stats = next(
        tf.compat.v1.io.tf_record_iterator(stats_tfrecord_path))
    stats.ParseFromString(serialized_stats)
    return stats
  else:
    logging.warning('No artifact found at %s or %s', stats_tfrecord_path,
                    feature_stats_path)
    return None


def read_metrics_eval_result(
    metrics_artifact_uri: Text,
    output_file_format: Optional[Text] = None) -> Optional[tfma.EvalResult]:
  """Reads TFMA evaluation results from the evaluator output path.

  Args:
    metrics_artifact_uri: the output artifact path of a TFMA component.
    output_file_format: an optional file format of the payload.

  Returns:
    A TFMA EvalResults named tuple including configs and sliced metrics.
    Returns None if no slicing metrics found from `metrics_artifact_uri`.
  """
  result = tfma.load_eval_result(
      output_path=metrics_artifact_uri, output_file_format=output_file_format)
  if not result.slicing_metrics:
    logging.warning('Cannot load eval results from: %s', metrics_artifact_uri)
    return None
  return result


def annotate_eval_result_metrics(model_card: model_card_module.ModelCard,
                                 eval_result: tfma.EvalResult):
  """Annotates model_card's PerformanceMetrics for every metric in eval_result.

  Args:
    model_card: The model card object.
    eval_result: A `tfma.EvalResult`.

  Raises:
    ValueError: if an unexpected metric or slice type is found.
  """

  for slice_repr, metrics_for_slice in (
      eval_result.get_metrics_for_all_slices().items()):
    # Parse the slice name
    if not isinstance(slice_repr, tuple):
      raise ValueError(
          'Expected EvalResult slices to be tuples; found %s' %
          type(slice_repr))
    slice_name = '_X_'.join(f'{a}_{b}' for a, b in slice_repr)
    for metric_name, metric_value in metrics_for_slice.items():
      # Parse the metric value
      if 'doubleValue' in metric_value:
        parsed_value = metric_value['doubleValue']
      elif 'boundedValue' in metric_value:
        parsed_value = metric_value['boundedValue']['value']
      else:
        raise ValueError('Expected doubleValue or boundedValue; found %s' %
                         metric_value.keys())
      # Create the PerformanceMetric and append to the ModelCard
      metric = model_card_module.PerformanceMetric(
          type=metric_name, value=str(parsed_value), slice=slice_name)
      model_card.quantitative_analysis.performance_metrics.append(metric)
