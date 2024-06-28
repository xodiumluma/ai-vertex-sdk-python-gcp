# This is the default test configuration override for python repos
# See https://github.com/GoogleCloudPlatform/python-docs-samples/blob/master/noxfile_config.py

TEST_CONFIG_OVERRIDE = {
  # opt out from specific Python versions for testing
  "ignored_versions": ["2.7", "3.6", "3.8", "3.9"],
  # populate at will
  "gcloud_project_env": "BUILD_SPECIFIC_GOOGLE_CLOUD_PROJECT",
}