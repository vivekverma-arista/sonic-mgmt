import pytest
import logging

from tests.common.utilities import skip_release
from tests.common.config_reload import config_reload
from tests.common.gu_utils import apply_patch, restore_backup_test_config, save_backup_test_config
from tests.common.gu_utils import generate_tmpfile, delete_tmpfile

CONFIG_DB = "/etc/sonic/config_db.json"
CONFIG_DB_BACKUP = "/etc/sonic/config_db.json.before_gcu_test"

logger = logging.getLogger(__name__)


@pytest.fixture(scope="module")
def selected_dut_hostname(request, rand_one_dut_hostname):
    """Fixture that returns either `rand_one_dut_hostname` or `rand_one_dut_front_end_hostname`
    depending on availability."""
    if "rand_one_dut_front_end_hostname" in request.fixturenames:
        logger.info("Running on front end duthost")
        return request.getfixturevalue("rand_one_dut_front_end_hostname")
    else:
        logger.info("Running on any type of duthost")
        return rand_one_dut_hostname


# Module Fixture
@pytest.fixture(scope="module")
def cfg_facts(duthosts, selected_dut_hostname, selected_asic_index):
    """
    Config facts for selected DUT
    Args:
        duthosts: list of DUTs.
        selected_dut_hostname: Hostname of a random chosen dut
        selected_asic_index: Random selected asic id
    """
    duthost = duthosts[selected_dut_hostname]
    asic_id = selected_asic_index
    asic_namespace = duthost.get_namespace_from_asic_id(asic_id)
    return duthost.config_facts(host=duthost.hostname, source="persistent", namespace=asic_namespace)['ansible_facts']


@pytest.fixture(scope="module", autouse=True)
def check_image_version(duthosts, selected_dut_hostname):
    """Skips this test if the SONiC image installed on DUT is older than 202111

    Args:
        duthosts: list of DUTs.
        selected_dut_hostname: Hostname of a random chosen dut

    Returns:
        None.
    """
    duthost = duthosts[selected_dut_hostname]
    skip_release(duthost, ["201811", "201911", "202012", "202106", "202111"])


@pytest.fixture(scope="module", autouse=True)
def reset_and_restore_test_environment(duthosts, selected_dut_hostname):
    """Reset and restore test env if initial Config cannot pass Yang

    Back up the existing config_db.json file and restore it once the test ends.

    Args:
        duthosts: list of DUTs.
        selected_dut_hostname: Hostname of a random chosen dut

    Returns:
        None.
    """
    duthost = duthosts[selected_dut_hostname]
    json_patch = []
    tmpfile = generate_tmpfile(duthost)

    try:
        output = apply_patch(duthost, json_data=json_patch, dest_file=tmpfile)
    finally:
        delete_tmpfile(duthost, tmpfile)

    save_backup_test_config(duthost, file_postfix="before_gcu_test")

    if output['rc'] or "Patch applied successfully" not in output['stdout']:
        logger.info("Running config failed SONiC Yang validation. Reload minigraph. config: {}"
                    .format(output['stdout']))
        config_reload(duthost, config_source="minigraph", safe_reload=True)

    yield

    restore_backup_test_config(duthost, file_postfix="before_gcu_test", config_reload=False)

    if output['rc'] or "Patch applied successfully" not in output['stdout']:
        logger.info("Restore Config after GCU test.")
        config_reload(duthost)


@pytest.fixture(scope="module", autouse=True)
def verify_configdb_with_empty_input(duthosts, selected_dut_hostname):
    """Fail immediately if empty input test failure

    Args:
        duthosts: list of DUTs.
        selected_dut_hostname: Hostname of a random chosen dut

    Returns:
        None.
    """
    duthost = duthosts[selected_dut_hostname]
    json_patch = []
    tmpfile = generate_tmpfile(duthost)

    try:
        output = apply_patch(duthost, json_data=json_patch, dest_file=tmpfile)
        if output['rc'] or "Patch applied successfully" not in output['stdout']:
            pytest.fail(
                "SETUP FAILURE: ConfigDB fail to validate Yang. rc:{} msg:{}"
                .format(output['rc'], output['stdout'])
            )

    finally:
        delete_tmpfile(duthost, tmpfile)


@pytest.fixture(scope='function')
def skip_when_buffer_is_dynamic_model(duthost):
    buffer_model = duthost.shell(
        'redis-cli -n 4 hget "DEVICE_METADATA|localhost" buffer_model')['stdout']
    if buffer_model == 'dynamic':
        pytest.skip("Skip the test, because dynamic buffer config cannot be updated")


# Function Fixture
@pytest.fixture(autouse=True)
def ignore_expected_loganalyzer_exceptions(duthosts, selected_dut_hostname, loganalyzer):
    """
       Ignore expected yang validation failure during test execution

       GCU will try several sortings of JsonPatch until the sorting passes yang validation

       Args:
            duthosts: list of DUTs.
            selected_dut_hostname: Hostname of a random chosen dut
           loganalyzer: Loganalyzer utility fixture
    """
    # When loganalyzer is disabled, the object could be None
    duthost = duthosts[selected_dut_hostname]
    if loganalyzer:
        ignoreRegex = [
            ".*ERR sonic_yang.*",
            ".*ERR.*Failed to start dhcp_relay.service - dhcp_relay container.*",  # Valid test_dhcp_relay for Bookworm
            ".*ERR.*Failed to start dhcp_relay container.*",  # Valid test_dhcp_relay
            # Valid test_dhcp_relay test_syslog
            ".*ERR GenericConfigUpdater: Service Validator: Service has been reset.*",
            ".*ERR teamd[0-9].*get_dump: Can't get dump for LAG.*",  # Valid test_portchannel_interface
            ".*ERR swss[0-9]*#intfmgrd: :- setIntfVrf:.*",  # Valid test_portchannel_interface
            ".*ERR swss[0-9]*#orchagent.*removeLag.*",  # Valid test_portchannel_interface
            ".*ERR kernel.*Reset adapter.*",  # Valid test_portchannel_interface replace mtu
            ".*ERR swss[0-9]*#orchagent: :- getPortOperSpeed.*",  # Valid test_portchannel_interface replace mtu
            ".*ERR systemd.*Failed to start Host core file uploader daemon.*",  # Valid test_syslog

            # sonic-swss/orchagent/crmorch.cpp
            ".*ERR swss[0-9]*#orchagent.*getResAvailableCounters.*",  # test_monitor_config
            ".*ERR swss[0-9]*#orchagent.*objectTypeGetAvailability.*",  # test_monitor_config
            ".*ERR dhcp_relay[0-9]*#dhcrelay.*",  # test_dhcp_relay

            # sonic-sairedis/vslib/HostInterfaceInfo.cpp: Need investigation
            ".*ERR syncd[0-9]*#syncd.*tap2veth_fun: failed to write to socket.*",   # test_portchannel_interface tc2
            ".*ERR.*'apply-patch' executed failed.*",  # negative cases that are expected to fail

            # Ignore errors from k8s config test
            ".*ERR ctrmgrd.py: Refer file.*",
            ".*ERR ctrmgrd.py: Join failed.*"
        ]
        loganalyzer[duthost.hostname].ignore_regex.extend(ignoreRegex)


@pytest.fixture(scope="session")
def skip_if_packet_trimming_not_supported(duthost):
    """
    Check if the current device supports packet trimming feature.
    """
    platform = duthost.facts["platform"]
    logger.info(f"Checking packet trimming support for platform: {platform}")

    # Check if the SWITCH_TRIMMING_CAPABLE capability is true
    trimming_capable = duthost.command('redis-cli -n 6 HGET "SWITCH_CAPABILITY|switch" "SWITCH_TRIMMING_CAPABLE"')[
        'stdout'].strip()
    if trimming_capable.lower() != 'true':
        pytest.skip("Packet trimming is not supported")
