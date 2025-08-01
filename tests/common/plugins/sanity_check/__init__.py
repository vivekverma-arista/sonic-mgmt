import logging
import copy
import json
from contextlib import contextmanager

import pytest

from collections import defaultdict

from tests.common.helpers.multi_thread_utils import SafeThreadPoolExecutor
from tests.common.helpers.parallel_utils import InitialCheckState, InitialCheckStatus
from tests.common.plugins.sanity_check import constants
from tests.common.plugins.sanity_check import checks
from tests.common.plugins.sanity_check.checks import *      # noqa: F401, F403
from tests.common.plugins.sanity_check.recover import recover, recover_chassis
from tests.common.plugins.sanity_check.constants import STAGE_PRE_TEST, STAGE_POST_TEST
from tests.common.helpers.assertions import pytest_assert as pt_assert
from tests.common.helpers.custom_msg_utils import add_custom_msg
from tests.common.helpers.constants import (
    DUT_CHECK_NAMESPACE
)

logger = logging.getLogger(__name__)

SUPPORTED_CHECKS = checks.CHECK_ITEMS
CUSTOM_MSG_PREFIX = "sonic_custom_msg"


def pytest_sessionfinish(session, exitstatus):

    pre_sanity_failed = session.config.cache.get("pre_sanity_check_failed", None)
    post_sanity_failed = session.config.cache.get("post_sanity_check_failed", None)

    if pre_sanity_failed:
        session.config.cache.set("pre_sanity_check_failed", None)
    if post_sanity_failed:
        session.config.cache.set("post_sanity_check_failed", None)

    if pre_sanity_failed and not post_sanity_failed:
        session.exitstatus = constants.PRE_SANITY_CHECK_FAILED_RC
    elif not pre_sanity_failed and post_sanity_failed:
        session.exitstatus = constants.POST_SANITY_CHECK_FAILED_RC
    elif pre_sanity_failed and post_sanity_failed:
        session.exitstatus = constants.SANITY_CHECK_FAILED_RC


def fallback_serializer(_):
    """
    Fallback serializer for non JSON serializable objects

    Used for json.dumps
    """
    return '<not serializable>'


def _update_check_items(old_items, new_items, supported_items):
    """
    @summary: Update the items to be performed in sanity check
    @param old_items: Existing items to be checked. Should be a Set.
    @param new_items: Iterable. Items to be added or removed.
    @param supported_items: The sanity check items that are currently supported.
    """
    updated_items = copy.deepcopy(old_items)
    for new_item in new_items:
        if not new_item:
            continue
        if new_item[0] in ["_", "-"]:      # Skip a check item
            new_item = new_item[1:]
            if new_item in updated_items:
                logger.info("Skip checking '%s'" % new_item)
                updated_items.remove(new_item)
        else:                       # Add a check item
            if new_item[0] == "+":
                new_item = new_item[1:]
            if new_item in supported_items:
                if new_item not in updated_items:
                    logger.info("Add checking '{}'".format(new_item))
                    updated_items.append(new_item)
            else:
                logger.warning('Check item "{}" no in supported check items: {}'.format(new_item, supported_items))
    return updated_items


def print_logs(duthosts, ptfhost, print_dual_tor_logs=False, check_ptf_mgmt=True):

    def print_cmds_output_from_duthost(dut, is_dual_tor, ptf):
        logger.info("Run commands to print logs")

        cmds = list(constants.PRINT_LOGS.values())

        if is_dual_tor is False:
            cmds.remove(constants.PRINT_LOGS['mux_status'])
            cmds.remove(constants.PRINT_LOGS['mux_config'])

        if check_ptf_mgmt:
            # check PTF device reachability
            if ptf.mgmt_ip:
                cmds.append("ping {} -c 1 -W 3".format(ptf.mgmt_ip))
                cmds.append("traceroute {}".format(ptf.mgmt_ip))

            if ptf.mgmt_ipv6:
                cmds.append("ping6 {} -c 1 -W 3".format(ptf.mgmt_ipv6))
                cmds.append("traceroute6 {}".format(ptf.mgmt_ipv6))

        results = dut.shell_cmds(cmds=cmds, module_ignore_errors=True, verbose=False)['results']
        outputs = []
        for res in results:
            res.pop('stdout')
            res.pop('stderr')
            outputs.append(res)
        logger.info("dut={}, cmd_outputs={}".format(dut.hostname, json.dumps(outputs, indent=4)))

    with SafeThreadPoolExecutor(max_workers=8) as executor:
        for duthost in duthosts:
            executor.submit(print_cmds_output_from_duthost, duthost, print_dual_tor_logs, ptfhost)


def filter_check_items(tbinfo, duthosts, check_items):
    filtered_check_items = copy.deepcopy(check_items)

    # ignore BGP check for particular topology type
    if tbinfo['topo']['type'] == 'ptf' and 'check_bgp' in filtered_check_items:
        filtered_check_items.remove('check_bgp')

    if 'dualtor' not in tbinfo['topo']['name'] and 'check_mux_simulator' in filtered_check_items:
        filtered_check_items.remove('check_mux_simulator')

    def _is_voq_chassis(duthosts):
        for duthost in duthosts:
            if duthost.facts['switch_type'] == "voq":
                return True
        return False

    if 'ft2' in tbinfo['topo']['name'] or \
        'lt2' in tbinfo['topo']['name'] or \
            't2' not in tbinfo['topo']['name'] or \
            _is_voq_chassis(duthosts):
        if 'check_bfd_up_count' in filtered_check_items:
            filtered_check_items.remove('check_bfd_up_count')
        if 'check_mac_entry_count' in filtered_check_items:
            filtered_check_items.remove('check_mac_entry_count')

    return filtered_check_items


def do_checks(request, check_items, *args, **kwargs):
    check_results = []
    for item in check_items:
        check_fixture = request.getfixturevalue(item)
        results = check_fixture(*args, **kwargs)
        logger.debug("check results of each item {}".format(results))
        if results and isinstance(results, list):
            check_results.extend(results)
        elif results:
            check_results.append(results)
    return check_results


@pytest.fixture(scope="module")
def prepare_parallel_run(request, parallel_run_context):
    is_par_run, target_hostname, is_par_leader, par_followers, par_state_file = parallel_run_context
    should_skip_sanity = False
    if is_par_run:
        initial_check_state = InitialCheckState(par_followers, par_state_file) if is_par_run else None
        if is_par_leader:
            initial_check_state.set_new_status(InitialCheckStatus.SETUP_STARTED, is_par_leader, target_hostname)

            yield should_skip_sanity

            if (request.config.cache.get("pre_sanity_check_failed", None) or
                    request.config.cache.get("post_sanity_check_failed", None)):
                initial_check_state.set_new_status(
                    InitialCheckStatus.SANITY_CHECK_FAILED,
                    is_par_leader,
                    target_hostname,
                )
            else:
                initial_check_state.set_new_status(
                    InitialCheckStatus.TEARDOWN_COMPLETED,
                    is_par_leader,
                    target_hostname,
                )

                initial_check_state.wait_for_all_acknowledgments(InitialCheckStatus.TEARDOWN_COMPLETED)
        else:
            should_skip_sanity = True
            logger.info(
                "Fixture sanity_check_full setup for non-leader nodes in parallel run is skipped. "
                "Please refer to the leader node log for check status."
            )

            yield should_skip_sanity

            logger.info(
                "Fixture sanity_check_full teardown for non-leader nodes in parallel run is skipped. "
                "Please refer to the leader node log for check status."
            )

            initial_check_state.wait_and_acknowledge_status(
                InitialCheckStatus.TEARDOWN_COMPLETED,
                is_par_leader,
                target_hostname,
            )
    else:
        yield should_skip_sanity


@pytest.fixture(scope="module")
def sanity_check_full(ptfhost, prepare_parallel_run, localhost, duthosts, request, fanouthosts, tbinfo):
    logger.info("Prepare sanity check")
    should_skip_sanity = prepare_parallel_run
    if should_skip_sanity:
        logger.info("Skip sanity check according to parallel run status")
        yield
        return

    skip_sanity = False
    allow_recover = False
    recover_method = "adaptive"
    pre_check_items = copy.deepcopy(SUPPORTED_CHECKS)  # Default check items
    post_check = False
    nbr_hosts = None

    customized_sanity_check = None
    for m in request.node.iter_markers():
        logger.info("Found marker: m.name=%s, m.args=%s, m.kwargs=%s" % (m.name, m.args, m.kwargs))
        if m.name == "sanity_check":
            customized_sanity_check = m
            break

    if customized_sanity_check:
        logger.info("Process marker {} in script. m.args={}, m.kwargs={}"
                    .format(customized_sanity_check.name, customized_sanity_check.args, customized_sanity_check.kwargs))
        skip_sanity = customized_sanity_check.kwargs.get("skip_sanity", False)
        allow_recover = customized_sanity_check.kwargs.get("allow_recover", False)
        recover_method = customized_sanity_check.kwargs.get("recover_method", "adaptive")
        if allow_recover and recover_method not in constants.RECOVER_METHODS:
            pytest.warning("Unsupported recover method")
            logger.info("Fall back to use default recover method 'config_reload'")
            recover_method = "config_reload"

        pre_check_items = _update_check_items(
            pre_check_items,
            customized_sanity_check.kwargs.get("check_items", []),
            SUPPORTED_CHECKS)

        post_check = customized_sanity_check.kwargs.get("post_check", False)

    if skip_sanity:
        logger.info("Skip sanity check according to configuration of test script.")
        yield
        return

    if request.config.option.allow_recover:
        allow_recover = True

    # Command line specified recover method has higher priority
    if request.config.option.recover_method:
        recover_method = request.config.getoption("--recover_method")

    if request.config.option.post_check:
        post_check = True

    if not request.config.option.enable_macsec:
        pre_check_items.remove("check_neighbor_macsec_empty")

    cli_check_items = request.config.getoption("--check_items")
    cli_post_check_items = request.config.getoption("--post_check_items")

    if cli_check_items:
        logger.info('Fine tune pre-test check items based on CLI option --check_items')
        cli_items_list = str(cli_check_items).split(',')
        pre_check_items = _update_check_items(pre_check_items, cli_items_list, SUPPORTED_CHECKS)

    pre_check_items = filter_check_items(tbinfo, duthosts, pre_check_items)  # Filter out un-supported checks.

    if post_check:
        # Prepare post test check items based on the collected pre test check items.
        post_check_items = copy.copy(pre_check_items)
        if customized_sanity_check:
            post_check_items = _update_check_items(
                post_check_items,
                customized_sanity_check.kwargs.get("post_check_items", []),
                SUPPORTED_CHECKS)

        if cli_post_check_items:
            logger.info('Fine tune post-test check items based on CLI option --post_check_items')
            cli_post_items_list = str(cli_post_check_items).split(',')
            post_check_items = _update_check_items(post_check_items, cli_post_items_list, SUPPORTED_CHECKS)

        post_check_items = filter_check_items(tbinfo, duthosts, post_check_items)  # Filter out un-supported checks.
    else:
        post_check_items = set()

    logger.info("Sanity check settings: skip_sanity=%s, pre_check_items=%s, allow_recover=%s, recover_method=%s, "
                "post_check=%s, post_check_items=%s" %
                (skip_sanity, pre_check_items, allow_recover, recover_method, post_check, post_check_items))

    pre_post_check_items = pre_check_items + [item for item in post_check_items if item not in pre_check_items]
    for item in pre_post_check_items:
        request.fixturenames.append(item)

        # Workaround for pytest requirement.
        # Each possibly used check fixture must be executed in setup phase. Otherwise there could be teardown error.
        request.getfixturevalue(item)

    if pre_check_items:
        logger.info("Start pre-test sanity checks")

        # Dynamically attach selected check fixtures to node
        for item in set(pre_check_items):
            request.fixturenames.append(item)
        dual_tor = 'dualtor' in tbinfo['topo']['name']
        print_logs(duthosts, ptfhost, print_dual_tor_logs=dual_tor)

        check_results = do_checks(request, pre_check_items, stage=STAGE_PRE_TEST)
        logger.debug("Pre-test sanity check results:\n%s" %
                     json.dumps(check_results, indent=4, default=fallback_serializer))

        failed_results = [result for result in check_results if result['failed']]
        if failed_results:
            add_custom_msg(request, f"{DUT_CHECK_NAMESPACE}.pre_sanity_check_failed", True)
            if not allow_recover:
                request.config.cache.set("pre_sanity_check_failed", True)
                pt_assert(False, "!!!!!!!!!!!!!!!!Pre-test sanity check failed: !!!!!!!!!!!!!!!!\n{}"
                          .format(json.dumps(failed_results, indent=4, default=fallback_serializer)))
            else:
                nbr_hosts = request.getfixturevalue('nbrhosts')
                recover_on_sanity_check_failure(ptfhost, duthosts, failed_results, fanouthosts, localhost, nbr_hosts,
                                                pre_check_items, recover_method, request, tbinfo, STAGE_PRE_TEST)

        logger.info("Done pre-test sanity check")
    else:
        logger.info('No pre-test sanity check item, skip pre-test sanity check.')

    yield

    if not post_check:
        logger.info("No post-test check is required. Done post-test sanity check")
    else:
        if post_check_items:
            logger.info("Start post-test sanity check")
            post_check_results = do_checks(request, post_check_items, stage=STAGE_POST_TEST)
            logger.debug("Post-test sanity check results:\n%s" %
                         json.dumps(post_check_results, indent=4, default=fallback_serializer))

        post_failed_results = [result for result in post_check_results if result['failed']]
        if post_failed_results:
            add_custom_msg(request, f"{DUT_CHECK_NAMESPACE}.post_sanity_check_failed", True)
            if not allow_recover:
                request.config.cache.set("post_sanity_check_failed", True)
                pt_assert(False, "!!!!!!!!!!!!!!!! Post-test sanity check failed: !!!!!!!!!!!!!!!!\n{}"
                          .format(json.dumps(post_failed_results, indent=4, default=fallback_serializer)))
            else:
                if not nbr_hosts:
                    nbr_hosts = request.getfixturevalue('nbrhosts')
                recover_on_sanity_check_failure(ptfhost, duthosts, post_failed_results, fanouthosts, localhost,
                                                nbr_hosts, post_check_items, recover_method, request, tbinfo,
                                                STAGE_POST_TEST)

            logger.info("Done post-test sanity check")
        else:
            logger.info('No post-test sanity check item, skip post-test sanity check.')


def recover_on_sanity_check_failure(ptfhost, duthosts, failed_results, fanouthosts, localhost, nbrhosts, check_items,
                                    recover_method, request, tbinfo, sanity_check_stage: str):
    sanity_failed_cache_key = "pre_sanity_check_failed"
    recovery_failed_cache_key = "pre_sanity_recovery_failed"
    if sanity_check_stage == STAGE_POST_TEST:
        sanity_failed_cache_key = "post_sanity_check_failed"
        recovery_failed_cache_key = "post_sanity_recovery_failed"

    try:
        dut_failed_results = defaultdict(list)
        infra_recovery_actions = []
        for failed_result in failed_results:
            if 'host' in failed_result:
                dut_failed_results[failed_result['host']].append(failed_result)
            if 'hosts' in failed_result:
                for hostname in failed_result['hosts']:
                    dut_failed_results[hostname].append(failed_result)
            if failed_result['check_item'] in constants.INFRA_CHECK_ITEMS:
                if 'action' in failed_result and failed_result['action'] is not None \
                        and callable(failed_result['action']):
                    infra_recovery_actions.append(failed_result['action'])
        for action in infra_recovery_actions:
            action()

        is_modular_chassis = duthosts[0].get_facts().get("modular_chassis")
        if is_modular_chassis:
            recover_chassis(duthosts)
        else:
            for dut_name, dut_results in list(dut_failed_results.items()):
                # Attempt to restore DUT state
                recover(ptfhost, duthosts[dut_name], localhost, fanouthosts, nbrhosts, tbinfo, dut_results,
                        recover_method)

    except BaseException as e:
        request.config.cache.set(sanity_failed_cache_key, True)
        add_custom_msg(request, f"{DUT_CHECK_NAMESPACE}.{recovery_failed_cache_key}", True)

        logger.error(f"Recovery of sanity check failed with exception: {repr(e)}")
        pt_assert(
            False,
            f"!!!!!!!!!!!!!!!! Recovery of sanity check failed !!!!!!!!!!!!!!!!"
            f"Exception: {repr(e)}"
        )
    logger.info("Run sanity check again after recovery")
    new_check_results = do_checks(request, check_items, stage=sanity_check_stage, after_recovery=True)
    logger.debug(f"{sanity_check_stage} sanity check after recovery results: \n%s" %
                 json.dumps(new_check_results, indent=4, default=fallback_serializer))
    new_failed_results = [result for result in new_check_results if result['failed']]
    if new_failed_results:
        request.config.cache.set(sanity_failed_cache_key, True)
        add_custom_msg(request, f"{DUT_CHECK_NAMESPACE}.{recovery_failed_cache_key}", True)
        pt_assert(False,
                  f"!!!!!!!!!!!!!!!! {sanity_check_stage} sanity check after recovery failed: !!!!!!!!!!!!!!!!\n"
                  f"{json.dumps(new_failed_results, indent=4, default=fallback_serializer)}")
    # Record recovery success
    add_custom_msg(request, f"{DUT_CHECK_NAMESPACE}.{recovery_failed_cache_key}", False)


def _sanity_check(request, parallel_run_context):

    is_par_run, target_hostname, is_par_leader, par_followers, par_state_file = parallel_run_context
    initial_check_state = InitialCheckState(par_followers, par_state_file) if is_par_run else None
    if is_par_run:
        initial_check_state.mark_and_wait_before_setup(target_hostname, is_par_leader)

    if request.config.option.skip_sanity:
        logger.info("Skip sanity check according to command line argument")
        if is_par_run and is_par_leader:
            initial_check_state.set_new_status(InitialCheckStatus.SETUP_STARTED, is_par_leader, target_hostname)

        yield

        if is_par_run:
            if is_par_leader:
                initial_check_state.set_new_status(
                    InitialCheckStatus.TEARDOWN_COMPLETED,
                    is_par_leader,
                    target_hostname,
                )

                initial_check_state.wait_for_all_acknowledgments(InitialCheckStatus.TEARDOWN_COMPLETED)
            else:
                initial_check_state.wait_and_acknowledge_status(
                    InitialCheckStatus.TEARDOWN_COMPLETED,
                    is_par_leader,
                    target_hostname,
                )
    else:
        yield request.getfixturevalue('sanity_check_full')


@pytest.fixture(scope="module", autouse=True)
def sanity_check(request, parallel_run_context):
    with contextmanager(_sanity_check)(request, parallel_run_context) as result:
        yield result
