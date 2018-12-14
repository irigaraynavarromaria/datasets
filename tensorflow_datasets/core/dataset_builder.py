# coding=utf-8
# Copyright 2018 The TensorFlow Datasets Authors.
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

"""DatasetBuilder base class."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import abc
import functools
import os
import re

import six
import tensorflow as tf

from tensorflow_datasets.core import api_utils
from tensorflow_datasets.core import constants
from tensorflow_datasets.core import dataset_utils
from tensorflow_datasets.core import download
from tensorflow_datasets.core import file_format_adapter
from tensorflow_datasets.core import naming
from tensorflow_datasets.core import registered
from tensorflow_datasets.core import splits
from tensorflow_datasets.core import units
from tensorflow_datasets.core import utils

import termcolor


FORCE_REDOWNLOAD = download.GenerateMode.FORCE_REDOWNLOAD
REUSE_CACHE_IF_EXISTS = download.GenerateMode.REUSE_CACHE_IF_EXISTS
REUSE_DATASET_IF_EXISTS = download.GenerateMode.REUSE_DATASET_IF_EXISTS


class BuilderConfig(object):
  """Base class for data configuration.

  DatasetBuilder subclasses with data configuration options should subclass
  `BuilderConfig` and add their own properties.
  """

  @api_utils.disallow_positional_args
  def __init__(self, name, version=None, description=None):
    self._name = name
    self._version = version
    self._description = description

  @property
  def name(self):
    return self._name

  @property
  def version(self):
    return self._version

  @property
  def description(self):
    return self._description

  def __repr__(self):
    return "<{cls_name} name={name}, version={version}>".format(
        cls_name=type(self).__name__,
        name=self.name,
        version=self.version or "None")


@six.add_metaclass(registered.RegisteredDataset)
class DatasetBuilder(object):
  """Abstract base class for datasets.

  Typical usage:

  ```python
  mnist_builder = tfds.MNIST(data_dir="~/tfds_data")
  mnist_builder.download_and_prepare()
  train_dataset = mnist_builder.as_dataset(tfds.Split.TRAIN)
  assert isinstance(train_dataset, tf.data.Dataset)

  # And then the rest of your input pipeline
  train_dataset = train_dataset.repeat().shuffle(1024).batch(128)
  train_dataset = train_dataset.prefetch(tf.data.experimental.AUTOTUNE)
  features = train_dataset.make_one_shot_iterator().get_next()
  image, label = features['image'], features['label']
  ```
  """

  # Name of the dataset, filled by metaclass based on class name.
  name = None
  # Semantic version of the dataset (ex: tfds.Version('1.2.0'))
  VERSION = None

  # TODO(b/120647158): Add module versioning module for backward compatibility.

  # Named configurations that modify the data generated by download_and_prepare.
  BUILDER_CONFIGS = []

  @api_utils.disallow_positional_args
  def __init__(self, data_dir=None, config=None, version=utils.Version.AUTO):
    """Construct a DatasetBuilder.

    Callers must pass arguments as keyword arguments.

    Args:
      data_dir: `str`, directory to read/write data. Defaults to
        "~/tensorflow_datasets".
      config: `tfds.core.BuilderConfig` or `str` name, optional configuration
        for the dataset that affects the data generated on disk. Different
        `builder_config`s will have their own subdirectories and versions.
      version: `tfds.Version`, the version to load. If the code isn't compatible
        with the version or the version does not exist in data_dir, an error
        will be raised.
        If `AUTO`, the latest version on disk will be used if any exist,
        otherwise the `LATEST` version will be used.
        If version is `LATEST` and the version on disk is not the latest,
        the data will be regenerated.
        An error might be raised if the version from disk is not compatible
        with the code.
    """
    # If the user doesn't specify anything, the version is automatically
    # inferred
    if version is None:
      version = utils.Version.AUTO
    # Automatically convert str to version
    version = utils.Version(version)

    # Dataset version either restored from data or code (VERSION or config)
    self._version = None

    # Create the builder config if exists
    self._builder_config = self._create_builder_config(config)

    self._data_dir_root = os.path.expanduser(data_dir or constants.DATA_DIR)

    # Get the last dataset if it exists (or None otherwise)
    self._data_dir = self._get_data_dir(version=version)

    # Choose the version to use (restored from data or use the code)
    self._set_version(version)

  def _set_version(self, version):
    """Backward compatible version manager (restore data or use code version).

    Version management to ensure backward compatibility. This function either
    restore the version from disk or ignore the disk and use only the
    code.

    Warning: Once this function has been called and previously generated
    data has been restored, the version is definitly set and it's not possible
    re-generate the data at the last version. A new builder instance should be
    created instead.

    Args:
      version: `tfds.Version`, The version to restore, or a LATEST or AUTO
        constant to auto-inference.

    """
    if not self._builder_config and not self.VERSION:
      raise AssertionError(
          "DatasetBuilder {} does not have defined version. Please add a "
          "`VERSION = tfds.Version('x.y.z')` to the class attribute".format(
              self.name))

    # Step 1: Extract code_version and data_version
    if self._data_dir:
      data_version = utils.Version(os.path.basename(self._data_dir))
    else:
      data_version = None

    if self._builder_config:
      code_version = utils.Version(self._builder_config.version)
    else:
      code_version = utils.Version(self.VERSION)

    # Step 2: Depending on the requested version, data version, code version,
    # choose whether use the code version (ignore data) or data
    # version (restore DatasetInfo,...)
    use_code = self._choose_version(
        requested_version=version,
        data_version=data_version,
        code_version=code_version
    )

    # Step 3: Ensure that version wasn't set before this point (this ensure
    # that no part of the code depends on version before version is properly
    # set)
    if self._version is not None:
      # Should never happend as self._version is initialized by this function
      raise AssertionError("Version as been defined, yet isn't restored yet.")

    # Step 4: Set version either to code or data and restore DatasetInfo
    if use_code:  # Use the code version (do not restore data)
      self._version = code_version
      # When using the code version, we explicitly reset the data_dir to None,
      # even if previously generated data exists.
      self._data_dir = None

      # If defined, add pre-computed info to DatasetInfo (num samples, splits,
      # ...)
      self.info.initialize_from_package_data()

    else:  # Use data version (restored from disk)
      # Now the version has been restored so can be used to set-up backward
      # compatible code.
      self._version = data_version

      # TODO(b/120647158): Module versioning should be resored !!

      # Overwrite the current dataset info with the restored data version.
      self.info.read_from_directory(self._data_dir)

  def _choose_version(self, requested_version, data_version, code_version):
    """Choose which version to use between data_version and code_version.

    There are three cases:
     * Restore the version from disk (restore previous DatasetInfo,...) (4):
       * If version=AUTO and previous data exists on disk (any version)
       * If version=LATEST and last version exists on disk
       * If version=custom and version exists
     * Do not restore version and use code version.
       * If version=AUTO or version=LATEST and no version is found on disk (1)
       * If version=LATEST and data on disk is not at the latest version (2)
     * Raise an error:
       * If a custom version is given but is not found on disk (3)

    Args:
      requested_version: `tfds.Version` requested by the user (AUTO, LATEST or
        custom)
      data_version: `tfds.Version`, data_version (or None if no previous data
        is found)
      code_version: `tfds.Version`, Code version (either from BuilderConfig
        DatasetBuilder.VERSION)

    Returns:
      use_code: `bool`, Return True is data_dir should be ignored and code
        version used. Return False if data_dir should be restored.
    """
    # 1. No data is found on disk (and mode is LATEST or AUTO)
    if (not data_version and
        requested_version in (utils.Version.AUTO, utils.Version.LATEST)):
      tf.logging.info(
          "No previous data found for %s. Using code version: %s",
          self.name,
          code_version,
      )
      use_code = True

    # 2. Data is found but correspond to a previous version (LATEST mode)
    elif (
        data_version and
        data_version < code_version and
        requested_version == utils.Version.LATEST
    ):
      tf.logging.info(
          "Ignoring dataset %s (version %s) from disk. Using code version: %s",
          self.name,
          data_version,
          code_version,
      )
      use_code = True

    # 3. No data is found on disk (but version has been explicitly set)
    elif not data_version:
      raise ValueError("Could not find the requested version at {}".format(
          self._data_dir))

    # 4. Data version defined and restored
    else:
      tf.logging.info(
          "Restoring dataset %s at version %s from %s",
          self.name,
          data_version,
          self._data_dir,
      )

      if code_version > data_version:
        tf.logging.warn(
            "WARNING: The restored dataset is at version %s but the code is at "
            "a more recent version %s. You may want to re-generate the data "
            "using tfds.builder(%s, version=tfds.Version.LATEST) followed by "
            "download_and_prepare().\n"
            "Entering in backward compatibility mode...",
            data_version,
            code_version,
            self.name,
        )

      if code_version < data_version:
        raise ValueError(
            "Error for dataset {name}: The dataset present in {data_dir} is at "
            "a higher version than the code (data version: {data_version} > "
            "code version: {code_version}). This probably means that you're "
            "using an old version of the code. You can try to explicitly load "
            "the data version from your code by explicitly using the version "
            "kwarg: tfds.builder({name}, version={code_version})."
            "".format(
                name=self.name,
                data_dir=self._data_dir,
                data_version=data_version,
                code_version=code_version,
            ))

      use_code = False

    return use_code

  @utils.memoized_property
  def info(self):
    """Return the dataset info object. See `DatasetInfo` for details."""
    # Ensure .info hasn't been called before versioning is set-up
    # Otherwise, backward compatibility cannot be guaranteed as some code will
    # depend on the code version instead of the restored data version
    if not getattr(self, "_version", None):
      # Message for developper creating new dataset. Will trigger if they are
      # using .info in the constructor before calling super().__init__
      raise AssertionError(
          "Info should not been called before version has been defined. "
          "Otherwise, the created .info may not match the info version from "
          "the restored dataset.")
    return self._info()

  @api_utils.disallow_positional_args
  def download_and_prepare(
      self,
      download_dir=None,
      extract_dir=None,
      manual_dir=None,
      mode=None,
      compute_stats=True):
    """Downloads and prepares dataset for reading.

    Subclasses must override _download_and_prepare.

    Args:
      download_dir: `str`, directory where downloaded files are stored.
        Defaults to "~/tensorflow-datasets/downloads".
      extract_dir: `str`, directory where extracted files are stored.
        Defaults to "~/tensorflow-datasets/extracted".
      manual_dir: `str`, read-only directory where manually downloaded/extracted
        data is stored. Defaults to
        "~/tensorflow-datasets/manual/{dataset_name}".
      mode: `tfds.GenerateMode`: Mode to FORCE_REDOWNLOAD,
        or REUSE_DATASET_IF_EXISTS. Defaults to REUSE_DATASET_IF_EXISTS.
      compute_stats: `boolean` If True, compute statistics over the generated
        data and write the `tfds.core.DatasetInfo` protobuf to disk.

    Raises:
      ValueError: If the user defines both cache_dir and dl_manager
    """

    mode = (mode and download.GenerateMode(mode)) or REUSE_DATASET_IF_EXISTS
    if (self._data_dir and mode == REUSE_DATASET_IF_EXISTS):
      tf.logging.info("Reusing dataset %s (%s)", self.name, self._data_dir)
      return

    if self._data_dir:
      raise ValueError(
          "DatasetBuilder cannot generate a dataset if it has already loaded "
          "data ({}). Otherwise, this could create conflict between version ("
          "ex: loaded info from version 1.0.0 but try to generate version 1.1.0"
          "). If you want to generate the data at a new version, please set "
          "version=tfds.Version.LATEST at construction time."
          "".format(self._data_dir)
      )

    dl_manager = self._make_download_manager(
        download_dir=download_dir,
        extract_dir=extract_dir,
        manual_dir=manual_dir,
        mode=mode)

    # Create a new version in a new data_dir.
    self._data_dir = self._get_data_dir(version=self.info.version)
    # Currently it's not possible to overwrite the data because it would
    # conflict with versioning: If the last version has already been generated,
    # it will always be reloaded and data_dir will be set at construction.
    if tf.gfile.Exists(self._data_dir):
      raise ValueError(
          "Trying to overwrite an existing dataset {} at {}. A dataset with "
          "the same version {} already exists. If the dataset has changed, "
          "please update the version number.".format(self.name, self._data_dir,
                                                     self.info.version))
    tf.logging.info("Generating dataset %s (%s)", self.name, self._data_dir)
    self._log_download_bytes()

    # Create a tmp dir and rename to self._data_dir on successful exit.
    with file_format_adapter.incomplete_dir(self._data_dir) as tmp_data_dir:
      # Temporarily assign _data_dir to tmp_data_dir to avoid having to forward
      # it to every sub function.
      with utils.temporary_assignment(self, "_data_dir", tmp_data_dir):
        self._download_and_prepare(dl_manager=dl_manager)

        # Update the DatasetInfo metadata by computing statistics from the data.
        if compute_stats:
          self.info.compute_dynamic_properties()

        # Write DatasetInfo to disk, even if we haven't computed the statistics.
        self.info.write_to_directory(self._data_dir)

  @api_utils.disallow_positional_args
  def as_dataset(self,
                 split,
                 batch_size=1,
                 shuffle_files=None,
                 as_supervised=False):
    """Constructs a `tf.data.Dataset`.

    Callers must pass arguments as keyword arguments.

    Subclasses must override _as_dataset.

    Args:
      split: `tfds.Split`, which subset of the data to read.
      batch_size: `int`, batch size. Note that variable-length features will
        be 0-padded if `batch_size > 1`. Users that want more custom behavior
        should use `batch_size=1` and use the `tf.data` API to construct a
        custom pipeline.
      shuffle_files: `bool` (optional), whether to shuffle the input files.
        Defaults to `True` if `split == tfds.Split.TRAIN` and `False` otherwise.
      as_supervised: `bool`, if `True`, the returned `tf.data.Dataset`
        will have a 2-tuple structure `(input, label)` according to
        `builder.info.supervised_keys`. If `False`, the default,
        the returned `tf.data.Dataset` will have a dictionary with all the
        features.

    Returns:
      `tf.data.Dataset`
    """
    if not self._data_dir:
      raise AssertionError(
          ("Dataset %s: could not find data in %s. Please make sure to call "
           "dataset_builder.download_and_prepare(), or pass download=True to "
           "tfds.load() before trying to access the tf.data.Dataset object."
          ) % (self.name, self._data_dir_root))

    if isinstance(split, six.string_types):
      split = splits.NamedSplit(split)

    if shuffle_files is None:
      # Shuffle files if training
      shuffle_files = split == splits.Split.TRAIN

    dataset = self._as_dataset(split=split, shuffle_files=shuffle_files)
    if batch_size > 1:
      # Use padded_batch so that features with unknown shape are supported.
      padded_shapes = self.info.features.shape
      dataset = dataset.padded_batch(batch_size, padded_shapes)

    if as_supervised:
      if not self.info.supervised_keys:
        raise ValueError(
            "as_supervised=True but %s does not support a supervised "
            "(input, label) structure." % self.name)
      input_f, target_f = self.info.supervised_keys
      dataset = dataset.map(lambda fs: (fs[input_f], fs[target_f]),
                            num_parallel_calls=tf.data.experimental.AUTOTUNE)

    dataset = dataset.prefetch(tf.data.experimental.AUTOTUNE)

    # If shuffling, allow pipeline to be non-deterministic
    options = tf.data.Options()
    options.experimental_deterministic = not shuffle_files
    dataset = dataset.with_options(options)
    return dataset

  def as_numpy(self, batch_size=1, **as_dataset_kwargs):
    # pylint: disable=g-doc-return-or-yield
    """Generates batches of NumPy arrays from the given `tfds.Split`.

    Args:
      batch_size: `int`, batch size for the NumPy arrays. If -1 or None,
        `as_numpy` will return the full dataset at once, each feature having its
        own array.
      **as_dataset_kwargs: Keyword arguments passed on to
        `tfds.core.DatasetBuilder.as_dataset`.

    Yields:
      Feature dictionaries
      `dict<str feature_name, numpy.array feature_val>`.

      If `batch_size` is -1 or None, will return a single dictionary containing
      the entire dataset instead of yielding batches.
    """
    # pylint: enable=g-doc-return-or-yield
    def _as_numpy(batch_size):
      """Internal as_numpy."""
      wants_full_dataset = batch_size == -1
      if wants_full_dataset:
        batch_size = self.info.num_examples or int(1e10)
      dataset = self.as_dataset(batch_size=batch_size, **as_dataset_kwargs)
      gen = dataset_utils.iterate_over_dataset(dataset)
      if wants_full_dataset:
        return next(gen)
      else:
        return gen

    if tf.executing_eagerly():
      return _as_numpy(batch_size)
    else:
      with tf.Graph().as_default():
        return _as_numpy(batch_size)

  def _get_data_dir(self, version=None):
    """Return the data directory of one dataset version.

    Args:
      version: (str) If specified, return the data_dir associated with the
        given version.

    Returns:
      data_dir: (str)
        If version is given, return the data_dir associated with this version.
        Otherwise, automatically extract the last version from the directory.
        If no previous version is found, return None.
    """
    if version in (utils.Version.LATEST, utils.Version.AUTO):
      version = None

    builder_config = self._builder_config
    builder_data_dir = os.path.join(self._data_dir_root, self.name)
    if builder_config:
      builder_data_dir = os.path.join(builder_data_dir, builder_config.name)
    if version:
      return os.path.join(builder_data_dir, str(version))

    if not tf.gfile.Exists(builder_data_dir):
      return None

    # Get the highest version directory
    version_dirnames = []
    for dir_name in tf.gfile.ListDirectory(builder_data_dir):
      try:
        version_dirnames.append((utils.Version(dir_name), dir_name))
      except ValueError:  # Invalid version (ex: incomplete data dir)
        pass
    # If found valid data directories, take the biggest version
    if version_dirnames:
      version_dirnames.sort(reverse=True)
      highest_version_dir = str(version_dirnames[0][1])
      return os.path.join(builder_data_dir, highest_version_dir)

    # No directory found
    return None

  def _log_download_bytes(self):
    # Print is intentional: we want this to always go to stdout so user has
    # information needed to cancel download/preparation if needed.
    # This comes right before the progress bar.
    size_text = units.size_str(self.info.size_in_bytes)
    termcolor.cprint(
        "Downloading / extracting dataset %s (%s) to %s..." %
        (self.name, size_text, self._data_dir),
        attrs=["bold"])
    # TODO(tfds): Should try to estimate the available free disk space (if
    # possible) and raise an error if not.

  @abc.abstractmethod
  def _info(self):
    """Construct the DatasetInfo object. See `DatasetInfo` for details.

    Warning: This function is only called once and the result is cached for all
    following .info() calls.

    Returns:
      dataset_info: (DatasetInfo) The dataset information
    """
    raise NotImplementedError

  @abc.abstractmethod
  def _download_and_prepare(self, dl_manager):
    """Downloads and prepares dataset for reading.

    This is the internal implementation to overwritte called when user call
    `download_and_prepare`. It should download all required data and generate
    the pre-processed datasets files.

    Args:
      dl_manager: (DownloadManager) `DownloadManager` used to download and cache
        data.
    """
    raise NotImplementedError

  @abc.abstractmethod
  def _as_dataset(self, split, shuffle_files=None):
    """Constructs a `tf.data.Dataset`.

    This is the internal implementation to overwritte called when user call
    `as_dataset`. It should read the pre-processed datasets files and generate
    the `tf.data.Dataset` object.

    Args:
      split (`tfds.Split`): which subset of the data to read.
      shuffle_files (bool): whether to shuffle the input files. Optional,
        defaults to `True` if `split == tfds.Split.TRAIN` and `False` otherwise.

    Returns:
      `tf.data.Dataset`
    """
    raise NotImplementedError

  def _make_download_manager(self, download_dir, extract_dir, manual_dir, mode):
    download_dir = download_dir or os.path.join(self._data_dir_root,
                                                "downloads")
    extract_dir = extract_dir or os.path.join(self._data_dir_root, "extracted")
    manual_dir = manual_dir or os.path.join(self._data_dir_root, "manual")
    manual_dir = os.path.join(manual_dir, self.name)

    return download.DownloadManager(
        dataset_name=self.name,
        checksums=self.info.download_checksums,
        download_dir=download_dir,
        extract_dir=extract_dir,
        manual_dir=manual_dir,
        force_download=(mode == FORCE_REDOWNLOAD),
        force_extraction=(mode == FORCE_REDOWNLOAD),
    )

  @property
  def builder_config(self):
    return self._builder_config

  def _create_builder_config(self, builder_config):
    """Create and validate BuilderConfig object."""
    if builder_config is None and self.BUILDER_CONFIGS:
      # Default to first config
      builder_config = self.BUILDER_CONFIGS[0]
    if not builder_config:
      return
    if isinstance(builder_config, six.string_types):
      name = builder_config
      builder_config = self.builder_configs.get(name)
      if builder_config is None:
        raise ValueError("BuilderConfig %s not found. Available: %s" %
                         (name, list(self.builder_configs.keys())))
    name = builder_config.name
    if not name:
      raise ValueError("BuilderConfig must have a name, got %s" % name)
    is_custom = name not in self.builder_configs
    if is_custom:
      tf.logging.warning("Using custom data configuration %s", name)
    else:
      if builder_config is not self.builder_configs[name]:
        raise ValueError(
            "Cannot name a custom BuilderConfig the same as an available "
            "BuilderConfig. Change the name. Available BuilderConfigs: %s" %
            (list(self.builder_configs.keys())))
      if not builder_config.version:
        raise ValueError("BuilderConfig %s must have a version" % name)
      if not builder_config.description:
        raise ValueError("BuilderConfig %s must have a description" % name)
    return builder_config

  @utils.classproperty
  @classmethod
  @utils.memoize()
  def builder_configs(cls):
    config_dict = {config.name: config for config in cls.BUILDER_CONFIGS}
    if len(config_dict) != len(cls.BUILDER_CONFIGS):
      names = [config.name for config in cls.BUILDER_CONFIGS]
      raise ValueError(
          "Names in BUILDER_CONFIGS must not be duplicated. Got %s" % names)
    return config_dict


class GeneratorBasedBuilder(DatasetBuilder):
  """Base class for datasets with data generation based on dict generators.

  `GeneratorBasedBuilder` is a convenience class that abstracts away much
  of the data writing and reading of `DatasetBuilder`. It expects subclasses to
  implement generators of feature dictionaries across the dataset splits
  (`_split_generators`) and to specify a file type
  (`_file_format_adapter`). See the method docstrings for details.

  Minimally, subclasses must override `_split_generators` and
  `_file_format_adapter`.

  `FileFormatAdapter`s are defined in
  `tensorflow_datasets.core.file_format_adapter` and specify constraints on the
  feature dictionaries yielded by example generators. See the class docstrings.
  """

  @api_utils.disallow_positional_args
  def __init__(self, **kwargs):
    """Builder constructor.

    Args:
      **kwargs: Constructor kwargs forwarded to DatasetBuilder
    """
    super(GeneratorBasedBuilder, self).__init__(**kwargs)

  @utils.memoized_property
  def _file_format_adapter(self):
    # Load the format adapter (CSV, TF-Record,...)
    file_adapter_cls = file_format_adapter.TFRecordExampleAdapter
    serialized_info = self.info.features.get_serialized_info()
    return file_adapter_cls(serialized_info)

  @abc.abstractmethod
  def _split_generators(self, dl_manager):
    """Specify feature dictionary generators and dataset splits.

    This function returns a list of `SplitGenerator`s defining how to generate
    data and what splits to use.

    Example:

      return[
          tfds.SplitGenerator(
              name=tfds.Split.TRAIN,
              num_shards=10,
              gen_kwargs={'file': 'train_data.zip'},
          ),
          tfds.SplitGenerator(
              name=tfds.Split.TEST,
              num_shards=5,
              gen_kwargs={'file': 'test_data.zip'},
          ),
      ]

    The above code will first call `_generate_examples(file='train_data.zip')`
    to write the train data, then `_generate_examples(file='test_data.zip')` to
    write the test data.

    Datasets are typically split into different subsets to be used at various
    stages of training and evaluation.

    Note that for datasets without a `VALIDATION` split, you can use a
    fraction of the `TRAIN` data for evaluation as you iterate on your model
    so as not to overfit to the `TEST` data.

    You can use a single generator shared between splits by providing list
    instead of values for `tfds.SplitGenerator` (this is the case if the
    underlying dataset does not have pre-defined data splits):

      return [tfds.SplitGenerator(
          name=[tfds.Split.TRAIN, tfds.Split.VALIDATION],
          num_shards=[10, 3],
      )]

    This will call `_generate_examples()` once but will automatically distribute
    the examples between train and validation set.
    The proportion of the examples that will end up in each split is defined
    by the relative number of shards each `ShardFiles` object specifies. In
    the previous case, the train split would contains 10/13 of the examples,
    while the validation split would contain 3/13.

    For downloads and extractions, use the given `download_manager`.
    Note that the `DownloadManager` caches downloads, so it is fine to have each
    generator attempt to download the source data.

    A good practice is to download all data in this function, and then
    distribute the relevant parts to each split with the `gen_kwargs` argument

    Args:
      dl_manager: (DownloadManager) Download manager to download the data

    Returns:
      `list<SplitGenerator>`.
    """
    raise NotImplementedError()

  @abc.abstractmethod
  def _generate_examples(self, **kwargs):
    """Default function generating examples for each `SplitGenerator`.

    This function preprocess the examples from the raw data to the preprocessed
    dataset files.
    This function is called once for each `SplitGenerator` defined in
    `_split_generators`. The examples yielded here will be written on
    disk.

    Args:
      **kwargs: (dict) Arguments forwarded from the SplitGenerator.gen_kwargs

    Yields:
      example: (`dict<str feature_name, feature_value>`), a feature dictionary
        ready to be written to disk. The example should usually be encoded with
        `self.info.features.encode_example({...})`.
    """
    raise NotImplementedError()

  def _download_and_prepare(self, dl_manager):
    if not tf.gfile.Exists(self._data_dir):
      tf.gfile.MakeDirs(self._data_dir)

    # Generating data for all splits
    split_dict = splits.SplitDict()
    for split_generator in self._split_generators(dl_manager):
      # Keep track of all split_info
      for s in split_generator.split_info_list:
        tf.logging.info("Generating split %s", s.name)
        split_dict.add(s)

      # Generate the filenames and write the example on disk
      generator_fn = functools.partial(self._generate_examples,
                                       **split_generator.gen_kwargs)
      output_files = self._build_split_filenames(
          split_info_list=split_generator.split_info_list,
      )
      self._file_format_adapter.write_from_generator(
          generator_fn,
          output_files,
      )

    # Update the info object with the splits.
    self.info.splits = split_dict

  def _as_dataset(self, split=splits.Split.TRAIN, shuffle_files=None):

    # Resolve all the named split tree by real ones
    read_instruction = split.get_read_instruction(self.info.splits)
    # Extract the list of SlicedSplitInfo objects containing the splits
    # to use and their associated slice
    list_sliced_split_info = read_instruction.get_list_sliced_split_info()
    # Resolve the SlicedSplitInfo objects into a list of
    # {'filepath': 'path/to/data-00032-00100', 'mask': [True, True, False, ...]}
    instruction_dicts = self._slice_split_info_to_instruction_dicts(
        list_sliced_split_info)

    # Load the dataset
    dataset = dataset_utils.build_dataset(
        instruction_dicts=instruction_dicts,
        dataset_from_file_fn=self._file_format_adapter.dataset_from_filename,
        shuffle_files=shuffle_files,
    )
    dataset = dataset.map(
        self.info.features.decode_example,
        num_parallel_calls=tf.data.experimental.AUTOTUNE)
    return dataset

  def _slice_split_info_to_instruction_dicts(self, list_sliced_split_info):
    """Return the list of files and reading mask of the files to read."""
    instruction_dicts = []
    for sliced_split_info in list_sliced_split_info:
      # Compute filenames from the given split
      for filepath in self._build_split_filenames(
          split_info_list=[sliced_split_info.split_info],
      ):
        mask = splits.slice_to_percent_mask(sliced_split_info.slice_value)
        instruction_dicts.append({
            "filepath": filepath,
            "mask": mask,
        })
    return instruction_dicts

  def _build_split_filenames(self, split_info_list):
    """Construct the split filenames associated with the split info.

    The filenames correspond to the pre-processed datasets files present in
    the root directory of the dataset.

    Args:
      split_info_list: (list[SplitInfo]) List of split from which generate the
        filenames

    Returns:
      filenames: (list[str]) The list of filenames path corresponding to the
        split info object
    """

    filenames = []
    for split_info in split_info_list:
      filenames.extend(naming.filepaths_for_dataset_split(
          dataset_name=self.name,
          split=split_info.name,
          num_shards=split_info.num_shards,
          data_dir=self._data_dir,
          filetype_suffix=self._file_format_adapter.filetype_suffix,
      ))
    return filenames
