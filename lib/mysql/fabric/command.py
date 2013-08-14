"""Module for supporting definition of Fabric commands.

This module aids in the definition of new commands to be incorporated
into the Fabric. Commands are defined as subclasses of the
:class:`Command` class and are automatically incorporated into the
client or server.

Commands have a *remote* and a *local* part, where the remote part is
executed by sending a request to the Fabric server for execution. The
local part of the command is executed by the client.

The default implementation of the local part just dispatch the command
to the server, so these commands do not need to define a local part at
all.

The documentation string of the command class is used as help text for
the command and can be shown using the "help" command. The first
sentence of the description is the brief description and is shown in
listings, while the remaining text is the more elaborate description
shown in command help message.
"""
import re
import inspect
import logging
import functools

import mysql.fabric.errors as _errors
import mysql.fabric.executor as _executor

from mysql.fabric.sharding import (
    MappingShardsGroups,
)

from mysql.fabric import (
    persistence as _persistence,
)

_LOGGER = logging.getLogger(__name__)

_COMMANDS_CLASS = {}

def register_command(group_name, command_name, command):
    """Register a command within a group.

    :param group_name: The command-group to which a command belongs.
    :param command_name: The command that needs to be registered.
    :param command: The command class that contains the implementation
                    for this command
    """
    commands = _COMMANDS_CLASS.setdefault(group_name, {})
    commands[command_name] = command

def unregister_command(group_name, command_name):
    """Unregister a command within a group.

    :param group_name: The command-group to which a command belongs.
    :param command_name: The command that needs to be registered.
    """
    del _COMMANDS_CLASS[group_name][command_name]
    if not _COMMANDS_CLASS[group_name]:
        del _COMMANDS_CLASS[group_name]

def get_groups():
    """Return registered groups of commands.

    :return: Returns the different command groups.
    """
    return _COMMANDS_CLASS.keys()

def get_commands(group_name):
    """Return registered commands within a group.

    :param group_name: The command group whose commands need to be listed.
    :return: The command classes that handles the command functionality.
    """
    return _COMMANDS_CLASS[group_name].keys()

def get_command(group_name, command_name):
    """Return a registered command within a group.

    :param group_name: The command group whose commands need to be listed.
    :param command_name: The command whose implementation needs to be fetched.
    :return: The command classes that handles the command functionality.
    """
    return _COMMANDS_CLASS[group_name][command_name]

class CommandMeta(type):
    """Metaclass for defining new commands.

    This class will register any new commands defined and add them to
    the a list of existing commands.
    """
    # TODO: Try to find a better way of removing false positives.
    IgnoredCommand = \
        ("command", "procedurecommand", "proceduregroup", "procedureshard")
    def __init__(cls, cname, cbases, cdict):
        """Register command definitions.
        """
        try:
            cls.group_name
        except AttributeError:
            cls.group_name = cdict["__module__"]

        try:
            cls.command_name
        except AttributeError:
            cls.command_name = cname.lower()

        if cls.command_name not in CommandMeta.IgnoredCommand and \
            re.match("[A-Za-z]\w+", cls.command_name):
            register_command(cls.group_name, cls.command_name, cls)

    @classmethod
    def _wrapfunc(cls, func, cname):
        """Wrap the a function in order to log when it started and
        finished its execution.
        """
        original = func
        @functools.wraps(func)
        def _wrap(*args, **kwrds):
            _LOGGER.debug("Started command (%s).", cname)
            ret = original(*args, **kwrds)
            _LOGGER.debug("Finished command (%s).", cname)
            return ret
        _wrap.original_function = func
        return _wrap

    def __new__(mcs, cname, cbases, cdict):
        """Wrap the execute function in order to log when it starts
        and finishes its execution.
        """
        for name, func in cdict.items():
            if name == "execute" and callable(func):
                cdict[name] = mcs._wrapfunc(func, cname)
        return type.__new__(mcs, cname, cbases, cdict)


class Command(object):
    """Base class for all commands.

    Each subclass implement both the server side and the client side
    of a command.

    When defining a command, implementing the execute method will
    allow execution on the server. If there is anything that needs to
    be done locally, before dispatching the command, it should be
    added to the dispatch method.

    Command instances automatically get a few attributes defined when
    being created. These can be accessed as normal attributes inside
    the command.

    On the client side, the following attributes are defined:

    options
       Any options provided to the command.
    config
       Any information provided through a configuration file.
    client
       A protocol client instance, which can be used to communicate
       with the server. This is normally not necessary, but can be
       used to get access to configuration file information.

    On the server side, the following attributes are defined:

    server
      The protocol server instance the command is set up for. The
      configuration file information can be accessed through this.

    Commands are organized into groups through the *group_name* class
    property. If it is not defined though, the module where the command
    is defined is used as the group name. Something similar happens to
    the command name, which means that if the *command_name* class
    property is not defined, the class name is automatically used.
    """
    __metaclass__ = CommandMeta

    command_options = []

    def __init__(self):
        self.__client = None
        self.__server = None
        self.__options = None
        self.__config = None

    @property
    def client(self):
        """Return the client proxy.
        """
        return self.__client

    @property
    def server(self):
        """Return the server proxy.
        """
        return self.__server

    @property
    def options(self):
        """Return command line options.
        """
        return self.__options

    @property
    def config(self):
        """Return configuration options.
        """
        return self.__config

    def setup_client(self, client, options, config):
        """Provide client-side information to the command.

        This is called after an instance of the command have been
        created on the client side and provide the client instance and
        options to the command.

        The client instance can be used to dispatch the command to the
        server.

        :param client: The client instance for the command.
        :param options: The options for the command.
        :param config: The configuration for the command.
        """
        assert self.__server is None
        self.__client = client
        self.__options = options
        self.__config = config

    def setup_server(self, server, options, config):
        """Provide server-side information to the command.

        This function is called after creating an instance of the
        command on the server-side and will set the server of the
        command. There will be one command instance for each protocol
        server available.

        :param server: Protocol server instance for the command.
        :param options: The options for the command.
        :param config: The configuration for the command.
        """
        assert self.__client is None
        self.__server = server
        self.__options = options
        self.__config = config

    def add_options(self, parser):
        """Method called to set up options from the class instance.

        :param parser: The parser used for parsing the command options.
        """
        try:
            for option in self.command_options:
                kwargs = option.copy()
                del kwargs['options']
                parser.add_option(*option['options'], **kwargs)
        except AttributeError:
            pass

    def dispatch(self, *args):
        """Default dispatch method, executed on the client side.

        The default dispatch method just call the server-side of the
        command.

        :param args: The arguments for the command dispatch.
        """
        status = self.client.dispatch(self, *args)
        return self.command_status(status)

    @staticmethod
    def command_status(status):
        """Present the result reported by a command in a friendly-user way.

        :param status: The command status.
        """
        string = [
            "Command :",
            "{ return = %s",
            "}"
            ]
        result = "\n".join(string)
        return result % (status, )


class ProcedureCommand(Command):
    # TODO: IMPROVE THE CODE SO USERS MAY DECIDE NOT TO USE WAIT_FOR_PROCEDURES AND
    # RETURN SOMETHING SIMPLE INSTEAD OF THE EXECUTION HISTORY ALONG WITH RETURN
    # VALUES.
    """Class used to implement commands that are built as procedures and
    schedule job(s) to be executed. Any command that needs to access the
    state store must be built upon this class.

    A procedure is asynchronously executed and schedules one or more jobs
    (i.e. functions) that are eventually processed. The scheduling is done
    through the executor which enqueues them and serializes their execution
    within a Fabric Server.

    Any job object encapsulates a function to be executed, its parameters,
    its execution's status and its result. Due to its asynchronous nature,
    a job accesses a snapshot produced by previously executed functions
    which are atomically processed so that Fabric is never left in an
    inconsistent state after a failure.

    To make it easy to use these commands, one might hide the asynchronous
    behavior by exploiting the :meth:`wait_for_procedures`.
    """
    def __init__(self):
        """Create the ProcedureCommand object.
        """
        super(ProcedureCommand, self).__init__()

    def dispatch(self, *args):
        """Default dispatch method when the command is build as a
        procedure.

        It calls command.dispatch, gets the result and processes
        it generating a user-friendly result.

        :param args: The arguments for the command dispatch.
        """
        status = self.client.dispatch(self, *args)
        return self.procedure_status(status)

    @staticmethod
    def wait_for_procedures(procedure_param, synchronous):
        """Wait until a procedure completes its execution and return
        detailed information on it.

        However, if the parameter synchronous is not set, only the
        procedure's uuid is returned because it is not safe to access
        the procedure's information while it may be executing.

        :param procedure_param: Iterable with procedures.
        :param synchronous: Whether should wait until the procedure
                            finishes its execution or not.
        :return: Information on the procedure.
        :rtype: str(procedure.uuid), procedure.status, procedure.result
                or (str(procedure.uuid))
        """
        assert(len(procedure_param) == 1)
        synchronous = str(synchronous).upper() not in ("FALSE", "0")
        if synchronous:
            executor = _executor.Executor()
            for procedure in procedure_param:
                executor.wait_for_procedure(procedure)
            return str(procedure_param[-1].uuid), procedure_param[-1].status, \
                procedure_param[-1].result
        else:
            return str(procedure_param[-1].uuid)

    def execute(self):
        """Any command derived from this class must redefine this
        method.
        """
        pass

    @staticmethod
    def procedure_status(status, details=False):
        """Transform a status reported by :func:`wait_for_procedures` into
        a string that can be used by the command-line interface.

        :param status: The status of the command execution.
        :param details: Boolean that indicates if detailed execution status
                        be returned.

        :return: Return the detailed execution status as a string.
        """
        string = [
            "Procedure :",
            "{ uuid        = %s,",
            "  finished    = %s,",
            "  success     = %s,",
            "  return      = %s,",
            "  activities  = %s",
            "}"
            ]
        result = "\n".join(string)

        if isinstance(status, str):
            return result % (status, "", "", "", "")

        proc_id = status[0]
        operation = status[1][-1]
        returned = None
        activities = ""
        complete = (operation["state"] == _executor.Job.COMPLETE)
        success = (operation["success"] == _executor.Job.SUCCESS)

        if success:
            returned = status[2]
            if details:
                steps = [step["description"] for step in status[1]]
                activities = "\n  ".join(steps)
        else:
            trace = operation["diagnosis"].split("\n")
            returned = trace[-2]
            if details:
                activities = "\n".join(trace)

        return result % (
            proc_id, complete, success, returned, activities
            )

    def get_lockable_objects(self, variable=None, function=None):
        """Return the set of lockable objects by extracting information
        on the parameter's value passed to the function.

        There are derived classes which return specific information according
        to the procedure that is being executed. This implementation returns
        a set with with the string "lock".

        :param variable: Paramater's name from which the value should be
                         extracted.
        :param function: Function where the parameter's value will be
                         searched for.
        """
        return set(["lock"])


class ProcedureGroup(ProcedureCommand):
    """Class used to implement commands that are built as procedures and
    execute operations within a group.
    """
    def get_lockable_objects(self, variable=None, function=None):
        """Return the set of lockable objects by extracting information
        on the parameter's value passed to the function.

        :param variable: Parameter's name from which the value should be
                         extracted.
        :param function: Function where the parameter's value will be
                         searched for.
        """
        variable = variable or "group_id"
        function = function or self.execute.original_function
        lockable_objects = set()
        # TODO: IS THERE A BETTER WAY TO GET THE FRAME?
        frame = inspect.currentframe().f_back

        args = _get_args_values((variable, ), function, frame)
        for variable, value in args.iteritems():
            lockable_objects.add(value)

        if len(lockable_objects) == 0:
            lockable_objects.add("lock")

        return lockable_objects


class ProcedureShard(ProcedureCommand):
    """Class used to implement commands that are built as procedures and
    execute operations within a sharding.
    """
    def get_lockable_objects(self, variable=None, function=None):
        """Return the set of lockable objects by extracting information
        on the parameter's value passed to the function.

        :param variable: Parameter's name from which the value should be
                         extracted.
        :param function: Function where the parameter's value will be
                         searched for.
        """
        # TODO: AddShard(ProcedureShard): The current design blocks all
        # groups associated with a shard_mapping_id while adding a shard.
        variable = variable or \
            ("group_id", "table_name", "shard_mapping_id", "shard_id")
        function = function or self.execute.original_function
        lockable_objects = set()
        # TODO: IS THERE A BETTER WAY TO GET THE FRAME?
        frame = inspect.currentframe().f_back

        persister = _persistence.current_persister()
        try:
            persister.begin()

            if not isinstance(variable, tuple):
                variable = (variable, )
            args = _get_args_values(variable, function, frame)
            for variable, value in args.iteritems():
                if variable == "group_id":
                    lockable_objects.add(value)
                    continue
                rows = MappingShardsGroups.get_group("local", variable, value)
                for row in rows:
                    lockable_objects.add(row[0])
                rows = MappingShardsGroups.get_group("global", variable, value)
                for row in rows:
                    lockable_objects.add(row[0])

            if len(lockable_objects) == 0:
                lockable_objects.add("lock")
        except Exception as error:
            # Report exception while fetching information on lockable objects.
            _LOGGER.exception(error)

            try:
                # Rollback the transactional context.
                persister.rollback()
            except _errors.DatabaseError as rollback_error:
                _LOGGER.exception(rollback_error)

            raise
        else:
            try:
                # Commit the transactional context.
                persister.commit()
            except _errors.DatabaseError as commit_error:
                _LOGGER.exception(commit_error)

        return lockable_objects


def _get_args_values(variables, function, frame):
    """Get the values for a set of variables, i.e. arguments.
    """
    args = {}
    argsspec = inspect.getargspec(function)
    if frame is None:
        return args
    # TODO: ADD ASSERTION THAT FRAME MUST MATCH FUNCTION.
    try:
        for variable in variables:
            if variable in argsspec.args:
                assert(args.get(variable, None) is None)
                value = inspect.getargvalues(frame).locals[variable]
                args[variable] = value
    finally:
        del frame
    return args
