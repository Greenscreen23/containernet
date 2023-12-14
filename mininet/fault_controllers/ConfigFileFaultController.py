"""ConfigFileFaultController implements a fault injector that defines faults from a single ocnfiguration file.
 For details, see FaultControllersREADME.md"""
import asyncio
import re
import atexit
import uuid

from ast import literal_eval
from multiprocessing import Pipe, Process

import yaml

from mininet import log
from mininet.faultlogger import FaultLogger
from mininet.node import Node
from mininet.fault_injectors import LinkInjector, NodeInjector

MESSAGE_SETUP_DONE = "m_faultinjector_ready"
MESSAGE_SETUP_ERROR = "m_faultinjector_setuperror"
MESSAGE_START_INJECTING = "m_faultinjector_go"
MESSAGE_INJECTION_DONE = "m_faultinjector_done"
MESSAGE_WRITE_LOGS = "m_write_logs"


class ConfigFileFaultControllerStarter():

    def __init__(self, net_reference: 'Mininet', filepath_to_config_file=None):
        self.net_reference = net_reference
        self.faults = []
        self.faults_are_active = False

        config = self._get_base_config_dict(filepath_to_config_file)
        agnostic_config = self._build_yml_with_mininet_agnostic_identifiers(self.net_reference, config)

        if 'log' in config:
            self.logger_active = True
        else:
            self.logger_active = False

        recv_pipe_mininet_to_faults, send_pipe_mininet_to_faults = Pipe()
        recv_pipe_faults_to_mininet, send_pipe_faults_to_mininet = Pipe()
        fault_process = Process(target=entrypoint_for_fault_controller, args=(
            agnostic_config, recv_pipe_mininet_to_faults, send_pipe_mininet_to_faults, recv_pipe_faults_to_mininet,
            send_pipe_faults_to_mininet))
        fault_process.start()

        self.send_pipe_mininet_to_faults = send_pipe_mininet_to_faults
        self.recv_pipe_mininet_to_faults = recv_pipe_mininet_to_faults
        self.send_pipe_faults_to_mininet = send_pipe_faults_to_mininet
        self.recv_pipe_faults_to_mininet = recv_pipe_faults_to_mininet

        log.debug("Fault process started\n")
        response = recv_pipe_faults_to_mininet.recv_bytes()
        log.debug("Received message from FI\n")
        # We need the second pipe,Otherwise we're getting interference from ourselves

        if response == MESSAGE_SETUP_DONE.encode():
            log.debug("FaultController has signalled that it's ready\n")
            return
        log.debug(f"FaultController has sent weird message: {response.decode()}\n")
        return

    def go(self):
        log.info("Initiating faults\n")
        self.faults_are_active = True
        self.send_pipe_mininet_to_faults.send_bytes(MESSAGE_START_INJECTING.encode())
        if self.logger_active:
            atexit.register(notify_to_write_to_logfile, self.send_pipe_mininet_to_faults)

    def is_active(self):
        if not self.faults_are_active:
            return False
        # Injector might have sent us "done" message
        if self.recv_pipe_faults_to_mininet.poll() is False:
            return True
        potential_done_message = self.recv_pipe_faults_to_mininet.recv_bytes()
        if potential_done_message == MESSAGE_INJECTION_DONE.encode():
            self.faults_are_active = False
            return False
        # This destroys the message in the pipe, but "I'm done injecting" is the only message we expect

        return True

    def _build_yml_with_mininet_agnostic_identifiers(self, net: 'Mininet', yml_config: dict) -> dict:
        for i, fault_object in enumerate(yml_config.get("faults")):
            # We expect a single key here, either link_fault or node_fault
            # Right now we don't care which one it is, so just get the first key
            fault_type = list(fault_object.keys())[0]
            fault_dict = fault_object.get(fault_type)

            new_identifier_strings = []
            for identifier_string in fault_dict.get("identifiers"):
                # Identifiers are in a->b or a->b:interface pattern, or in "a" node pattern
                node_identifying_tuple = ConfigFileFaultControllerStarter._get_mininet_agnostic_identifiers_from_identifier_string(
                    net, identifier_string)
                new_identifier_strings.append((repr(node_identifying_tuple)))
            fault_dict['identifiers'] = new_identifier_strings

            # If it's a 'redirect' fault, we also need to enrich the redirect-to interface, in the fault_type_args
            if fault_dict.get('type') == "link_fault:redirect":
                fault_type_args = fault_dict.get("type_args")
                potential_interface_name = fault_type_args[0]

                # it's either the interface name, or in the node->node (or node->node:interface) pattern
                need_to_extract_interface_name = self._is_string_in_arrow_pattern(net, potential_interface_name)
                if need_to_extract_interface_name:
                    interface_name, _ = self._get_node_and_interface_name_from_identifier_string(net,
                                                                                                 potential_interface_name)
                else:
                    interface_name = potential_interface_name

                yml_config['faults'][i]['link_fault']['type_args'][0] = interface_name
        if 'log' not in yml_config:
            # Caller doesn't want logs
            return yml_config
        commands = yml_config['log'].get("commands")
        if commands is None:
            return yml_config
        for i, debug_command in enumerate(commands):

            tag = debug_command.get('tag', None)
            if tag is None:
                tag = str(uuid.uuid4())
                yml_config['log']['commands'][i]['tag'] = str(tag)

            host_string = debug_command.get("host", None)
            node_identifying_tuple = self._get_mininet_agnostic_identifiers_from_identifier_string(net, host_string)
            yml_config['log']['commands'][i]['host'] = node_identifying_tuple[0]
        return yml_config

    def _get_base_config_dict(self, filepath_to_config_file):
        if filepath_to_config_file is None:
            log.error("Filepath to config file is missing")
            return None
        with open(filepath_to_config_file, 'r') as file:
            config = yaml.safe_load(file)
        return config

    @staticmethod
    def _get_mininet_agnostic_identifiers_from_identifier_string(net: 'Mininet', identifier_string: str) -> (
            int, str, str, str):
        """Takes a string in our node presentation, which can either be a node name (h1), arrow notation (h1->s1),
        or arrow notation with interfaces (h1->s1:eth0)"""
        corresponding_interface_name, corresponding_host = ConfigFileFaultControllerStarter._get_node_and_interface_name_from_identifier_string(
            net, identifier_string)
        process_group_id, interface_name = ConfigFileFaultControllerStarter._get_passable_identifiers_from_node_and_interface_name(
            corresponding_interface_name, corresponding_host)
        return process_group_id, interface_name, identifier_string

    @staticmethod
    def _is_string_in_arrow_pattern(net: 'Mininet', identifier_string) -> bool:
        implicit_link_regex = "^(\w*)->(\w*)$"  # matches "host_name->host_name"
        explicit_link_regex = "^(\w*)->(\w*):(\w*)$"  # matches "host_name->host_name:interface_name", useful if more than one link exists
        if match := re.match(implicit_link_regex, identifier_string):
            return True
        if match := re.match(explicit_link_regex, identifier_string):
            return True
        return False

    @staticmethod
    def _get_node_and_interface_name_from_identifier_string(net: 'Mininet', identifier_string) -> (str, Node):
        # These patterns are expected for link_fault s
        implicit_link_regex = "^(\w*)->(\w*)$"  # matches "host_name->host_name"
        explicit_link_regex = "^(\w*)->(\w*):(\w*)$"  # matches "host_name->host_name:interface_name", useful if more than one link exists
        if identifier_string is None:
            # This can happen for e.g. log commands, that don't need to be executed on a specific host
            return None, None
        if match := re.match(implicit_link_regex, identifier_string):
            nodename_a = match.groups()[0]
            nodename_b = match.groups()[1]
            explicit_name = None
        elif match := re.match(explicit_link_regex, identifier_string):
            nodename_a = match.groups()[0]
            nodename_b = match.groups()[1]
            explicit_name = match.groups()[2]
        else:
            # The identifier is in node pattern, so just the name of the node, no interface required.
            # Used for node injections, where there is no interface/second node
            nodename_a = identifier_string
            nodename_b = None
            explicit_name = None

        # Create a lookup dict and update it when appropriate if this is a performance bottleneck
        corresponding_interface_name = None
        corresponding_host = None

        if nodename_b is None:
            # not looking for an interface name, so we can skip that part
            for node in net.hosts:
                # Running over hosts _should_ be fine, since switches (usually) run in the root namespace
                # (and None defaults to the root namespace in the fault injectors)
                # If that doesn't work add in the switches/etc. into this list.
                if node.name == nodename_a:
                    return None, node

        for link in net.links:
            if link.intf1.node.name == nodename_a and link.intf2.node.name == nodename_b:
                corresponding_interface_name = link.intf1.name
                corresponding_host = link.intf1.node
                # If we're looking for a specific interface stop searching only if interface names match
                if explicit_name:
                    if explicit_name == corresponding_interface_name:
                        break
                    corresponding_interface_name = None
                    corresponding_host = None
            elif link.intf1.node.name == nodename_b and link.intf2.node.name == nodename_a:
                corresponding_interface_name = link.intf2.name
                corresponding_host = link.intf2.node
                # If we're looking for a specific interface stop searching only if interface names match
                if explicit_name:
                    if explicit_name == corresponding_interface_name:
                        break
                    corresponding_interface_name = None
                    corresponding_host = None

        if corresponding_interface_name is None:
            if explicit_name:
                log.warn(
                    f"Couldn't find interface {explicit_name} between hosts {nodename_a} and {nodename_b}. Are all names correct?...\n")
            else:
                log.warn(
                    f"Couldn't find fitting interface between hosts {nodename_a} and {nodename_b}. Are both names correct?...\n")
            return None, None

        return corresponding_interface_name, corresponding_host

    @staticmethod
    def _get_passable_identifiers_from_node_and_interface_name(corresponding_interface_name: str,
                                                               corresponding_node: Node):
        # Returns a tuple of (pgid, interface_name)#
        # process group id
        if corresponding_node is None:
            return None, None
        process_group_id = corresponding_node.pid  # If nodes assume this we can also assume it
        interface_name = corresponding_interface_name
        return process_group_id, interface_name


class ConfigFileFaultController:
    def __init__(self, agnostic_config, recv_pipe_mininet_to_faults, send_pipe_mininet_to_faults,
                 recv_pipe_faults_to_mininet, send_pipe_faults_to_mininet):
        self.config = agnostic_config
        self.faults = []  # set in configByFile
        self.fault_logger = None  # set in config_logger

        self.recv_pipe_mininet_to_faults = recv_pipe_mininet_to_faults
        self.send_pipe_mininet_to_faults = send_pipe_mininet_to_faults

        self.recv_pipe_faults_to_mininet = recv_pipe_faults_to_mininet
        self.send_pipe_faults_to_mininet = send_pipe_faults_to_mininet

        self._configByFile(self.config)
        self._config_logger(self.config)

        log.debug("FI: Sending setup finished command\n")
        self.send_pipe_faults_to_mininet.send_bytes(MESSAGE_SETUP_DONE.encode())

    def wait_until_go(self):
        log.info("Fault injector is waiting for go command\n")
        potential_go_message = self.recv_pipe_mininet_to_faults.recv_bytes()
        if potential_go_message == MESSAGE_START_INJECTING.encode():
            asyncio.run(self.go())

    async def go(self):
        log.debug("Initiating faults\n")

        fault_coroutines = []
        for i in self.faults:
            fault_coroutines.append(i.go())
        if self.fault_logger is not None:
            log_task = asyncio.create_task(self.fault_logger.go())
            log_task_activator = self.listen_for_log_write()
        log.debug("All faults scheduled.\n")


        await asyncio.gather(*fault_coroutines)
        self.send_pipe_faults_to_mininet.send_bytes(MESSAGE_INJECTION_DONE.encode())
        if self.fault_logger is not None:
            self.fault_logger.stop()
            await log_task
            await log_task_activator
        # All faults have finished injecting, so send the "done" message

    async def listen_for_log_write(self):
        while not self.recv_pipe_mininet_to_faults.poll():
            continue
        potential_go_message = self.recv_pipe_mininet_to_faults.recv_bytes()
        if potential_go_message == MESSAGE_WRITE_LOGS.encode():
            if self.fault_logger is not None:
                self.fault_logger.stop()
        else:
            log.error("Received unexpected message while waiting for log-to-file message\n")

    def _config_logger(self, config):
        log_config = config.get("log", None)
        if log_config is None:
            self.fault_logger = None
            return
        interval = int(log_config.get("interval", 0))
        if interval == 0:
            interval = None
        path = log_config.get("path", None)
        commands = log_config.get('commands', [])

        fault_logger = FaultLogger(interval=interval, log_filepath=path, commands=commands)
        self.fault_logger = fault_logger

    def _configByFile(self, config):
        """Reconfigures this controller according to the given file """

        self.faults = []

        for fault_object in config.get("faults"):
            fault_type = list(fault_object.keys())[0]
            fault_dict = fault_object.get(fault_type)

            # fault_target_protocol, src_port, dst_port
            fault_target_protocol, src_port, dst_port = self._get_target_arguments_from_fault_dict(
                fault_dict)

            # type, pattern, type_arg, pattern_arg
            fault_args = fault_dict.get('type_args', None)
            tag = fault_dict.get('tag', None)
            if tag is None:
                tag = str(uuid.uuid4())


            fault_pattern = fault_dict.get('pattern', 'persistent')
            fault_pattern_args = fault_dict.get('pattern_args', None)

            # pre_injection_time, injection_time, post_injection_time
            pre_injection_time = fault_dict.get('pre_injection_time', None)
            injection_time = fault_dict.get('injection_time', None)
            post_injection_time = fault_dict.get('post_injection_time', None)

            # fault type - this also decides which injector we use
            if (fault_type_value := fault_dict.get('type', None)) is None:
                log.warn("No fault type set\n")
                continue

            link_fault_regex = "^link_fault:(\w*)$"
            node_fault_regex = "^node_fault:(\w*)$"

            if match := re.match(link_fault_regex, fault_type_value):
                fault_type = match.groups()[0]

                # target_nics, target_node
                for identifier_string in fault_dict.get("identifiers"):
                    identifier_tuple = literal_eval(identifier_string)
                    node_process_pid = identifier_tuple[0]
                    corresponding_interface_name = identifier_tuple[1]
                    node_string_reference = identifier_tuple[2]
                    actual_tag = tag + "@" + node_string_reference

                    injector = LinkInjector(target_interface=corresponding_interface_name,
                                            target_namespace_pid=node_process_pid,
                                            tag=actual_tag,

                                            fault_target_protocol=fault_target_protocol,
                                            fault_target_dst_ports=dst_port,
                                            fault_target_src_ports=src_port,

                                            fault_type=fault_type,
                                            fault_pattern=fault_pattern,
                                            fault_args=fault_args,
                                            fault_pattern_args=fault_pattern_args,

                                            pre_injection_time=pre_injection_time,
                                            injection_time=injection_time,
                                            post_injection_time=post_injection_time)
                    self.faults.append(injector)
            elif match := re.match(node_fault_regex, fault_type_value):
                fault_type = match.groups()[0]
                # target_nics, target_node
                for identifier_string in fault_dict.get("identifiers"):
                    identifier_tuple = literal_eval(identifier_string)
                    node_process_pid = identifier_tuple[0]
                    node_string_reference = identifier_tuple[2]
                    actual_tag = tag + "@" + node_string_reference

                    injector = NodeInjector(
                        target_process_pid=node_process_pid,
                        fault_type=fault_type,
                        tag=actual_tag,
                        pre_injection_time=pre_injection_time,
                        injection_time=injection_time,
                        post_injection_time=post_injection_time,

                        fault_pattern=fault_pattern,
                        fault_args=fault_args,
                        fault_pattern_args=fault_pattern_args)
                    self.faults.append(injector)
            else:
                log.warn(f"Fault type unknown:'{fault_type_value}'\n")

    def _get_target_arguments_from_fault_dict(self, fault_dict):
        if 'target_traffic' in fault_dict:
            traffic_object = fault_dict.get('target_traffic')

            fault_target_protocol = traffic_object.get('protocol', 'any')
            if fault_target_protocol not in ['ICMP', 'IGMP', 'IP', 'TCP', 'UDP', 'IPv6', 'IPv6-ICMP', 'any']:
                log.error(f"Fault target protocol {fault_target_protocol} is unknown, injecting any instead")
                fault_target_protocol = 'any'

            src_port = traffic_object.get('src_port', None)
            dst_port = traffic_object.get('dst_port', None)
        else:
            fault_target_protocol = 'any'
            src_port = None
            dst_port = None

        return fault_target_protocol, src_port, dst_port


def entrypoint_for_fault_controller(mininet_agnostic_faultconfig: dict, recv_pipe_mininet_to_faults,
                                    send_pipe_mininet_to_faults, recv_pipe_faults_to_mininet,
                                    send_pipe_faults_to_mininet):
    main_injector = ConfigFileFaultController(mininet_agnostic_faultconfig, recv_pipe_mininet_to_faults,
                                              send_pipe_mininet_to_faults, recv_pipe_faults_to_mininet,
                                              send_pipe_faults_to_mininet)
    main_injector.wait_until_go()


def notify_to_write_to_logfile(pipe):
    pipe.send_bytes(MESSAGE_WRITE_LOGS.encode())
