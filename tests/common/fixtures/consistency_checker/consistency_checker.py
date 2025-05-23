import pytest
import logging
import json
import os
import datetime
from typing import List, Optional
from collections import defaultdict
from tests.common.fixtures.consistency_checker.constants import SUPPORTED_PLATFORMS_AND_VERSIONS, \
    ConsistencyCheckQueryKey, ALL_ATTRIBUTES

logger = logging.getLogger(__name__)

SYNCD_CONTAINER = "syncd"
QUERY_ASIC_SCRIPT = "query-asic.py"
QUERY_ASIC_PARSER = "parser.py"
LIBSAIREDIS_DEB = "libsairedis.deb"
PYTHON3_PYSAIREDIS_DEB = "python3-pysairedis.deb"
DUT_DST_PATH_HOST = "/tmp/consistency-checker"
DUT_DST_PATH_CONTAINER = "/consistency-checker"

QUERY_ASIC_PATH_SRC = os.path.dirname(__file__) + "/query-asic"
QUERY_ASIC_SCRIPT_PATH_SRC = QUERY_ASIC_PATH_SRC + "/" + QUERY_ASIC_SCRIPT
QUERY_ASIC_PARSER_PATH_SRC = QUERY_ASIC_PATH_SRC + "/" + QUERY_ASIC_PARSER
QUERY_ASIC_SCRIPT_PATH_DST_HOST = DUT_DST_PATH_HOST + "/" + QUERY_ASIC_SCRIPT
QUERY_ASIC_PARSER_PATH_DST_HOST = DUT_DST_PATH_HOST + "/" + QUERY_ASIC_PARSER
QUERY_ASIC_SCRIPT_PATH_DST_CONTAINER = DUT_DST_PATH_CONTAINER + "/" + QUERY_ASIC_SCRIPT

LIBSAIREDIS_TEMP = "libsairedis-temp"


class ConsistencyChecker:

    def __init__(self, duthost, libsairedis_download_url=None, python3_pysairedis_download_url=None):
        """
        If the libsairedis_download_url and python3_pysairedis_download_url are provided, then these artifacts
        are downloaded and installed on the DUT, otherwise it's assumed that the environment is already setup
        for the consistency checker.
        """
        self._duthost = duthost
        self._libsairedis_download_url = libsairedis_download_url
        self._python3_pysairedis_download_url = python3_pysairedis_download_url

    def __enter__(self):
        logger.info("Initializing consistency checker on dut...")

        self._duthost.file(path=DUT_DST_PATH_HOST, state="directory")
        self._duthost.copy(src=QUERY_ASIC_SCRIPT_PATH_SRC, dest=QUERY_ASIC_SCRIPT_PATH_DST_HOST)
        self._duthost.copy(src=QUERY_ASIC_PARSER_PATH_SRC, dest=QUERY_ASIC_PARSER_PATH_DST_HOST)

        if self._libsairedis_download_url is not None:
            self._duthost.command(f"curl -o {DUT_DST_PATH_HOST}/{LIBSAIREDIS_DEB} {self._libsairedis_download_url}")
        if self._python3_pysairedis_download_url is not None:
            self._duthost.command(
                f"curl -o {DUT_DST_PATH_HOST}/{PYTHON3_PYSAIREDIS_DEB} {self._python3_pysairedis_download_url}")

        # Move everything into syncd container
        self._duthost.shell((
            f"docker cp {DUT_DST_PATH_HOST} {SYNCD_CONTAINER}:/ && "
            f"rm -rf {DUT_DST_PATH_HOST}"
        ))

        if self._python3_pysairedis_download_url is not None:
            # Install python3-sairedis in syncd container
            self._duthost.shell((f"docker exec {SYNCD_CONTAINER} bash -c "
                                 f"'cd {DUT_DST_PATH_CONTAINER} && "
                                 f"dpkg --install {DUT_DST_PATH_CONTAINER}/{PYTHON3_PYSAIREDIS_DEB}'"))

        if self._libsairedis_download_url is not None:
            # Extract the libsairedis deb to be used by the query script
            self._duthost.shell((f"docker exec {SYNCD_CONTAINER} bash -c "
                                 f"'cd {DUT_DST_PATH_CONTAINER} && "
                                 f"dpkg --extract {DUT_DST_PATH_CONTAINER}/{LIBSAIREDIS_DEB} {LIBSAIREDIS_TEMP}'"))

        logger.info("Consistency checker setup complete.")

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        logger.info("Cleaning up consistency checker on dut...")

        if self._python3_pysairedis_download_url is not None:
            # Uninstall python3-sairedis in syncd container
            self._duthost.command(f"docker exec {SYNCD_CONTAINER} dpkg --remove python3-pysairedis")

        # Remove all the files from the syncd container
        self._duthost.command(f"docker exec {SYNCD_CONTAINER} rm -rf {DUT_DST_PATH_CONTAINER}")

        # NOTE: If consistency checker is used to do write operations (currently it's read-only), then syncd should be
        #       restarted or minigraph reloaded re-align the ASIC_DB and ASIC state.

        logger.info("Consistency checker cleanup complete.")

    def get_db_and_asic_peers(self, keys=["*"]) -> dict:
        """
        Bulk query ASIC data that exists in the ASIC_DB.

        :param keys: Optional list of glob search strings that correspond to the --key arg of sonic-db-dump.
                     sonic-db-dump doesn't take multiple keys, so a list is passed in to support multiple
                     keys at the API level.
        :return: Dictionary containing the queried ASIC data.

        Example return value:
        {
            "ASIC_STATE:SAI_OBJECT_TYPE_BUFFER_PROFILE:oid:0x1900000000154f": {
                "SAI_BUFFER_PROFILE_ATTR_POOL_ID": {
                    "dbValue": "oid:0x1800000000154a",
                    "asicValue": "oid:0x1800000000154a",
                    "asicQuerySuccess": True
                },
                "SAI_BUFFER_PROFILE_ATTR_SHARED_DYNAMIC_TH": {
                    "dbValue": "0",
                    "asicValue": -1,
                    "asicQuerySuccess": False,
                    "asicQueryErrorMsg": "Failed to query attribute value"
                },
                "SAI_BUFFER_PROFILE_ATTR_THRESHOLD_MODE": {
                    "dbValue": "SAI_BUFFER_PROFILE_THRESHOLD_MODE_DYNAMIC",
                    "asicValue": "SAI_BUFFER_PROFILE_THRESHOLD_MODE_DYNAMIC",
                    "asicQuerySuccess": True
                },
                ...
            },
            ...
        }
        """

        db_attributes = self._get_db_attributes(keys)
        asic_attributes = self._get_asic_attributes_from_db_results(db_attributes)

        results = defaultdict(dict)

        for object in db_attributes:
            db_object = db_attributes[object]
            asic_object = asic_attributes[object]

            for attr in db_object["value"].keys():
                db_value = db_object["value"][attr]
                asic_value = asic_object[attr]["asicValue"]

                if db_value.startswith("oid:0x"):
                    # Convert the asic one to the same format
                    try:
                        asic_value = f"oid:{hex(int(asic_value))}"
                    except Exception:
                        # keep the value as is
                        pass

                results[object][attr] = {
                    "dbValue": db_value,
                    "asicValue": asic_value,
                    "asicQuerySuccess": asic_object[attr]["success"]
                }

                if not asic_object[attr]["success"]:
                    results[object][attr]["asicQueryErrorMsg"] = asic_object[attr]["error"]

        return dict(results)

    def check_consistency(self, keys=None) -> dict:
        """
        Get the out-of-sync ASIC_DB and ASIC attributes. Differences are indicative of an error state.
        Same arg style as the get_objects function but returns a list of objects that don't match or couldn't
        be queried from the ASIC. If it was successfully queried and has a matching value, then it won't be
        included in the response.

        :param keys: Optional list of glob search strings that correspond to the --key arg of sonic-db-dump.
                     sonic-db-dump doesn't take multiple keys, so a list is passed in to support multiple
                     keys at the API level. If not provided, then the default keys are used.
        :return: Dictionary containing the out-of-sync ASIC_DB and ASIC attributes.

        Example return val (matching):
            {}

        Example return val (mismatch):
            {
                "ASIC_STATE:SAI_OBJECT_TYPE_BUFFER_PROFILE:oid:0x1900000000154f": {
                    "attributes": {
                        "SAI_BUFFER_PROFILE_ATTR_SHARED_DYNAMIC_TH": {
                            "dbValue": "0",
                            "asicValue": -1,
                        },
                        "SAI_BUFFER_PROFILE_ATTR_THRESHOLD_MODE": {
                            "dbValue": "SAI_BUFFER_PROFILE_THRESHOLD_MODE_DYNAMIC",
                            "asicValue": "SAI_BUFFER_PROFILE_THRESHOLD_MODE_STATIC"
                        },
                        ...
                    },
                    "failedToQueryAsic": [
                        {"SAI_BUFFER_PROFILE_ATTR_SHARED_DYNAMIC_TH": "Failed to query attribute value"}
                    ],
                    "mismatchedAttributes": ["SAI_BUFFER_PROFILE_ATTR_THRESHOLD_MODE"],
                    "attributeNotImplemented": ["SAI_BUFFER_PROFILE_ATTR_POOL_ID"]
                },
                ...
            }
        """
        if keys is None:
            platform = self._duthost.facts['platform']
            os_version = self._duthost.image_facts()["ansible_facts"]["ansible_image_facts"]["current"]
            keys = self._get_consistency_checker_keys(platform, os_version)

        db_attributes = self._get_db_attributes(keys)
        asic_attributes = self._get_asic_attributes_from_db_results(db_attributes)

        inconsistencies = defaultdict(lambda: {
            "attributes": {},
            "failedToQueryAsic": [],
            "mismatchedAttributes": [],
            "attributeNotImplemented": [],
        })

        for object in db_attributes:
            db_object = db_attributes[object]
            asic_object = asic_attributes[object]

            for attr in db_object["value"].keys():
                db_value = db_object["value"][attr]
                asic_value = asic_object[attr]["asicValue"]
                asic_query_success = asic_object[attr]["success"]

                if asic_query_success and db_value == asic_value:
                    continue

                if db_value.startswith("oid:0x"):
                    # Convert the asic one to the same format
                    try:
                        asic_value = f"oid:{hex(int(asic_value))}"
                        if db_value == asic_value:
                            continue
                    except Exception:
                        # true error - let below code handle it
                        pass

                inconsistencies[object]["attributes"][attr] = {
                    "dbValue": db_value,
                    "asicValue": asic_value
                }

                if asic_query_success:
                    inconsistencies[object]["mismatchedAttributes"].append(attr)
                else:
                    error = asic_object[attr]["error"]
                    if "ATTR_NOT_IMPLEMENTED" in error:
                        inconsistencies[object]["attributeNotImplemented"].append(attr)
                    else:
                        inconsistencies[object]["failedToQueryAsic"].append({attr: error})

        return dict(inconsistencies)

    def _get_consistency_checker_keys(self, platform, os_version) -> List[str]:
        """
        Get the keys for the given platform and OS version.

        :param platform: Platform name
        :param os_version: OS version
        :return: List of keys
        """

        if platform not in SUPPORTED_PLATFORMS_AND_VERSIONS:
            raise Exception(f"Unsupported platform: {platform}")

        supported_versions = SUPPORTED_PLATFORMS_AND_VERSIONS[platform]
        for version in supported_versions:
            if version in os_version:
                return supported_versions[version]

        raise Exception(f"Unsupported OS version: {os_version}")

    def _get_db_attributes(self, keys: List[ConsistencyCheckQueryKey]) -> dict:
        """
        Fetchs and merges the attributes of the objects returned by the search key from the DB.
        """
        db_attributes = {}
        for key in keys:
            result = self._duthost.command(f"sonic-db-dump -k '{key.key}' -n ASIC_DB")
            if result['rc'] != 0:
                raise Exception((f"Failed to fetch attributes for key '{key}' from ASIC_DB. "
                                 f"Return code: {result['rc']}, stdout: {result['stdout']}, "
                                 f"stderr: {result['stderr']}"))

            query_result = json.loads(result['stdout'])

            # Filter for attributes that we want ...
            objects_with_no_attrs = []
            for object in query_result:

                if "NULL" in query_result[object]["value"]:
                    logger.debug(f"Ignoring attribute 'NULL' for object '{object}'")
                    del query_result[object]["value"]["NULL"]

                if ALL_ATTRIBUTES in key.attributes:
                    logger.debug(f"Retaining all attributes for object '{object}'")
                else:
                    attributes_to_remove = set(query_result[object]["value"].keys()) - set(key.attributes)
                    for attr in attributes_to_remove:
                        logger.debug(f"Ignoring attribute '{attr}' for object '{object}'")
                        del query_result[object]["value"][attr]

                if len(query_result[object]["value"]) == 0:
                    objects_with_no_attrs.append(object)

            # ... then remove the objects that have no attributes left
            for object in objects_with_no_attrs:
                logger.debug(f"Ignoring empty object '{object}'")
                del query_result[object]

            db_attributes.update(query_result)

        return db_attributes

    def _get_asic_attributes_from_db_results(self, db_attributes: dict) -> dict:
        """
        Queries the ASIC for the attributes of the objects in db_attributes which are the results
        from the ASIC DB query.

        Example return value:
            {
                "ASIC_STATE:SAI_OBJECT_TYPE_BUFFER_POOL:oid:0x18000000000628": {
                    "SAI_BUFFER_POOL_ATTR_THRESHOLD_MODE": {
                        "asicValue": "SAI_BUFFER_POOL_THRESHOLD_MODE_STATIC",
                        "success": true
                    },
                    "SAI_BUFFER_POOL_ATTR_SIZE": {
                        "success" false,
                        "error": "Failed to query attribute value"
                    },
                    "SAI_BUFFER_POOL_ATTR_TYPE": {
                        "asicValue": "SAI_BUFFER_POOL_TYPE_EGRESS",
                        "success": true
                    }
                },
                ...
            }
        """
        # Map to format expected by the query-asic.py
        asic_query = {k: list(v["value"].keys()) for k, v in db_attributes.items()}
        asic_query_input_filename = f"query-input-{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}.json"
        with open(f"/tmp/{asic_query_input_filename}", 'w') as f:
            json.dump(asic_query, f)

        # Copy the input file to the syncd container
        self._duthost.copy(src=f"/tmp/{asic_query_input_filename}", dest=f"/tmp/{asic_query_input_filename}")
        self._duthost.shell((f"docker cp /tmp/{asic_query_input_filename} "
                             f"{SYNCD_CONTAINER}:{DUT_DST_PATH_CONTAINER} && "
                             f"rm /tmp/{asic_query_input_filename}"))

        ld_lib_path_arg = f"LD_LIBRARY_PATH={LIBSAIREDIS_TEMP}/usr/lib/x86_64-linux-gnu"\
                          if self._libsairedis_download_url is not None else ""

        res = self._duthost.shell((f"docker exec {SYNCD_CONTAINER} bash -c "
                                   f"'cd {DUT_DST_PATH_CONTAINER} && "
                                   f"{ld_lib_path_arg} python3 {QUERY_ASIC_SCRIPT_PATH_DST_CONTAINER} "
                                   f"--input {asic_query_input_filename}'"))
        if res['rc'] != 0:
            raise Exception((f"Failed to query ASIC attributes. Return code: {res['rc']}, stdout: {res['stdout']}, "
                             f"stderr: {res['stderr']}"))
        asic_results = json.loads(res['stdout'])

        return asic_results


class ConsistencyCheckerProvider:

    def __init__(self, libsairedis_url_template: Optional[str],
                 python3_pysairedis_url_template:  Optional[str]) -> None:
        """
        The libsairedis_url_template and python3_pysairedis_url_template are optional URL templates that the
        consistency checker can use to download the libsairedis and python3-pysairedis debs respectively.

        :param libsairedis_url_template: Optional URL template for the libsairedis deb
        :param python3_pysairedis_url_template: Optional URL template for the python3-pysairedis deb
        """
        self._libsairedis_url_template = libsairedis_url_template
        self._python3_pysairedis_url_template = python3_pysairedis_url_template

    def is_consistency_check_supported(self, dut) -> bool:
        """
        Checks if the provided DUT is supported for consistency checking.

        :param dut: SonicHost object
        :return bool: True if the DUT is supported, False otherwise
        """

        platform = dut.facts['platform']
        if platform not in SUPPORTED_PLATFORMS_AND_VERSIONS:
            return False

        current_version = dut.image_facts()['ansible_facts']['ansible_image_facts']['current']
        supported_versions = SUPPORTED_PLATFORMS_AND_VERSIONS[platform]
        if any(v in current_version for v in supported_versions.keys()):
            return True

        return False

    def get_consistency_checker(self, dut) -> ConsistencyChecker:
        """
        Get a new instance of the ConsistencyChecker class.

        :param dut: SonicHost object
        :return ConsistencyChecker: New instance of the ConsistencyChecker class
        """

        os_version = dut.image_facts()["ansible_facts"]["ansible_image_facts"]["current"]

        if self._libsairedis_url_template or self._python3_pysairedis_url_template:
            if "202305" in os_version:
                sonic_version_template_param = "202305"
            elif "202311" in os_version:
                sonic_version_template_param = "202311"
            else:
                raise Exception(f"Unsupported OS version: {os_version}")

        libsairedis_download_url = self._libsairedis_url_template\
            .format(sonic_version=sonic_version_template_param)\
            if self._libsairedis_url_template else None

        python3_pysairedis_download_url = self._python3_pysairedis_url_template\
            .format(sonic_version=sonic_version_template_param)\
            if self._python3_pysairedis_url_template else None

        return ConsistencyChecker(dut, libsairedis_download_url, python3_pysairedis_download_url)


@pytest.fixture
def consistency_checker_provider(request):
    """
    Fixture that provides the ConsistencyCheckerProvider class.

    :param request: pytest request object
    """

    if not request.config.getoption("enable_consistency_checker"):
        logger.info("Consistency checker is not enabled. Skipping check.")
        return None

    consistency_checker_libsairedis_url_template = request.config.getoption(
        "consistency_checker_libsairedis_url_template")
    consistency_checker_python3_pysairedis_url_template = request.config.getoption(
        "consistency_checker_python3_pysairedis_url_template")

    return ConsistencyCheckerProvider(consistency_checker_libsairedis_url_template,
                                      consistency_checker_python3_pysairedis_url_template)
