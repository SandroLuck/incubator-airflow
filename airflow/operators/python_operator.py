# -*- coding: utf-8 -*-
#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

from builtins import str
import dill
import inspect
import os
import pickle
import subprocess
import sys
import types

from airflow.exceptions import AirflowException
from airflow.models import BaseOperator, SkipMixin
from airflow.utils.decorators import apply_defaults
from airflow.utils.file import TemporaryDirectory

from textwrap import dedent


class PythonOperator(BaseOperator):
    """
    Executes a Python callable

    :param python_callable: A reference to an object that is callable
    :type python_callable: python callable
    :param op_kwargs: a dictionary of keyword arguments that will get unpacked
        in your function
    :type op_kwargs: dict
    :param op_args: a list of positional arguments that will get unpacked when
        calling your callable
    :type op_args: list
    :param provide_context: if set to true, Airflow will pass a set of
        keyword arguments that can be used in your function. This set of
        kwargs correspond exactly to what you can use in your jinja
        templates. For this to work, you need to define `**kwargs` in your
        function header.
    :type provide_context: bool
    :param templates_dict: a dictionary where the values are templates that
        will get templated by the Airflow engine sometime between
        ``__init__`` and ``execute`` takes place and are made available
        in your callable's context after the template has been applied. (templated)
    :type templates_dict: dict of str
    :param templates_exts: a list of file extensions to resolve while
        processing templated fields, for examples ``['.sql', '.hql']``
    :type templates_exts: list(str)
    """
    template_fields = ('templates_dict',)
    template_ext = tuple()
    ui_color = '#ffefeb'

    @apply_defaults
    def __init__(
            self,
            python_callable,
            op_args=None,
            op_kwargs=None,
            provide_context=False,
            templates_dict=None,
            templates_exts=None,
            *args, **kwargs):
        super(PythonOperator, self).__init__(*args, **kwargs)
        if not callable(python_callable):
            raise AirflowException('`python_callable` param must be callable')
        self.python_callable = python_callable
        self.op_args = op_args or []
        self.op_kwargs = op_kwargs or {}
        self.provide_context = provide_context
        self.templates_dict = templates_dict
        if templates_exts:
            self.template_ext = templates_exts

    def execute(self, context):
        if self.provide_context:
            context.update(self.op_kwargs)
            context['templates_dict'] = self.templates_dict
            self.op_kwargs = context

        return_value = self.execute_callable()
        self.log.info("Done. Returned value was: %s", return_value)
        return return_value

    def execute_callable(self):
        return self.python_callable(*self.op_args, **self.op_kwargs)


class BranchPythonOperator(PythonOperator, SkipMixin):
    """
    Allows a workflow to "branch" or follow a single path following the
    execution of this task.

    It derives the PythonOperator and expects a Python function that returns
    the task_id to follow. The task_id returned should point to a task
    directly downstream from {self}. All other "branches" or
    directly downstream tasks are marked with a state of ``skipped``,
    unless the downstream tasks are also a downstream task of
    the task_id to follow, so that these paths can't move forward.
    The ``skipped`` states are propagated downstream to allow for the
    DAG state to fill up and the DAG run's state to be inferred.

    Note that using tasks with ``depends_on_past=True`` downstream from
    ``BranchPythonOperator`` is logically unsound as ``skipped`` status
    will invariably lead to block tasks that depend on their past successes.
    ``skipped`` states propagates where all directly upstream tasks are
    ``skipped``.
    """
    def execute(self, context):
        branch = super(BranchPythonOperator, self).execute(context)
        self.log.info("Following branch %s", branch)
        self.log.info("Marking other directly downstream tasks as skipped")

        downstream_tasks = context['task'].downstream_list
        self.log.debug("Downstream task_ids %s", downstream_tasks)
        # Avoid skipping tasks which are in the downstream of the branch we are taking
        branch_downstream_tasks = context['dag'].get_task(branch).downstream_list
        skip_tasks = [t for t in downstream_tasks if t.task_id != branch]
        # Filter tasks which are also downstream tasks of the branch we are taking
        skip_tasks = [t for t in skip_tasks if t.task_id not in branch_downstream_tasks]
        self.log.debug("Downstream tasks which we will skip, task_ids %s", skip_tasks)
        if downstream_tasks:
            self.skip(context['dag_run'], context['ti'].execution_date, skip_tasks)

        self.log.info("Done.")


class ShortCircuitOperator(PythonOperator, SkipMixin):
    """
    Allows a workflow to continue only if a condition is met. Otherwise, the
    workflow "short-circuits" and downstream tasks are skipped.

    The ShortCircuitOperator is derived from the PythonOperator. It evaluates a
    condition and short-circuits the workflow if the condition is False. Any
    downstream tasks are marked with a state of "skipped". If the condition is
    True, downstream tasks proceed as normal.

    The condition is determined by the result of `python_callable`.
    """
    def execute(self, context):
        condition = super(ShortCircuitOperator, self).execute(context)
        self.log.info("Condition result is %s", condition)

        if condition:
            self.log.info('Proceeding with downstream tasks...')
            return

        self.log.info('Skipping downstream tasks...')

        downstream_tasks = context['task'].get_flat_relatives(upstream=False)
        self.log.debug("Downstream task_ids %s", downstream_tasks)

        if downstream_tasks:
            self.skip(context['dag_run'], context['ti'].execution_date, downstream_tasks)

        self.log.info("Done.")


class PythonVirtualenvOperator(PythonOperator):
    """
    Allows one to run a function in a virtualenv that is created and destroyed
    automatically (with certain caveats).

    The function must be defined using def, and not be
    part of a class. All imports must happen inside the function
    and no variables outside of the scope may be referenced. A global scope
    variable named virtualenv_string_args will be available (populated by
    string_args). In addition, one can pass stuff through op_args and op_kwargs, and one
    can use a return value.

    Note that if your virtualenv runs in a different Python major version than Airflow,
    you cannot use return values, op_args, or op_kwargs. You can use string_args though.

    :param python_callable: A python function with no references to outside variables,
        defined with def, which will be run in a virtualenv
    :type python_callable: function
    :param requirements: A list of requirements as specified in a pip install command
    :type requirements: list(str)
    :param python_version: The Python version to run the virtualenv with. Note that
        both 2 and 2.7 are acceptable forms.
    :type python_version: str
    :param use_dill: Whether to use dill to serialize
        the args and result (pickle is default). This allows more complex types
        but requires you to include dill in your requirements.
    :type use_dill: bool
    :param system_site_packages: Whether to include
        system_site_packages in your virtualenv.
        See virtualenv documentation for more information.
    :type system_site_packages: bool
    :param op_args: A list of positional arguments to pass to python_callable.
    :type op_kwargs: list
    :param op_kwargs: A dict of keyword arguments to pass to python_callable.
    :type op_kwargs: dict
    :param string_args: Strings that are present in the global var virtualenv_string_args,
        available to python_callable at runtime as a list(str). Note that args are split
        by newline.
    :type string_args: list(str)
    :param templates_dict: a dictionary where the values are templates that
        will get templated by the Airflow engine sometime between
        ``__init__`` and ``execute`` takes place and are made available
        in your callable's context after the template has been applied
    :type templates_dict: dict of str
    :param templates_exts: a list of file extensions to resolve while
        processing templated fields, for examples ``['.sql', '.hql']``
    :type templates_exts: list(str)
    """
    def __init__(self, python_callable,
                 requirements=None,
                 python_version=None, use_dill=False,
                 system_site_packages=True,
                 op_args=None, op_kwargs=None, string_args=None,
                 templates_dict=None, templates_exts=None, *args, **kwargs):
        super(PythonVirtualenvOperator, self).__init__(
            python_callable=python_callable,
            op_args=op_args,
            op_kwargs=op_kwargs,
            templates_dict=templates_dict,
            templates_exts=templates_exts,
            provide_context=False,
            *args,
            **kwargs)
        self.requirements = requirements or []
        self.string_args = string_args or []
        self.python_version = python_version
        self.use_dill = use_dill
        self.system_site_packages = system_site_packages
        # check that dill is present if needed
        dill_in_requirements = map(lambda x: x.lower().startswith('dill'),
                                   self.requirements)
        if (not system_site_packages) and use_dill and not any(dill_in_requirements):
            raise AirflowException('If using dill, dill must be in the environment ' +
                                   'either via system_site_packages or requirements')
        # check that a function is passed, and that it is not a lambda
        if (not isinstance(self.python_callable,
                           types.FunctionType) or (self.python_callable.__name__ ==
                                                   (lambda x: 0).__name__)):
            raise AirflowException('{} only supports functions for python_callable arg',
                                   self.__class__.__name__)
        # check that args are passed iff python major version matches
        if (python_version is not None and
                str(python_version)[0] != str(sys.version_info[0]) and
                self._pass_op_args()):
            raise AirflowException("Passing op_args or op_kwargs is not supported across "
                                   "different Python major versions "
                                   "for PythonVirtualenvOperator. "
                                   "Please use string_args.")

    def execute_callable(self):
        with TemporaryDirectory(prefix='venv') as tmp_dir:
            if self.templates_dict:
                self.op_kwargs['templates_dict'] = self.templates_dict
            # generate filenames
            input_filename = os.path.join(tmp_dir, 'script.in')
            output_filename = os.path.join(tmp_dir, 'script.out')
            string_args_filename = os.path.join(tmp_dir, 'string_args.txt')
            script_filename = os.path.join(tmp_dir, 'script.py')

            # set up virtualenv
            self._execute_in_subprocess(self._generate_virtualenv_cmd(tmp_dir))
            cmd = self._generate_pip_install_cmd(tmp_dir)
            if cmd:
                self._execute_in_subprocess(cmd)

            self._write_args(input_filename)
            self._write_script(script_filename)
            self._write_string_args(string_args_filename)

            # execute command in virtualenv
            self._execute_in_subprocess(
                self._generate_python_cmd(tmp_dir,
                                          script_filename,
                                          input_filename,
                                          output_filename,
                                          string_args_filename))
            return self._read_result(output_filename)

    def _pass_op_args(self):
        # we should only pass op_args if any are given to us
        return len(self.op_args) + len(self.op_kwargs) > 0

    def _execute_in_subprocess(self, cmd):
        try:
            self.log.info("Executing cmd\n{}".format(cmd))
            output = subprocess.check_output(cmd,
                                             stderr=subprocess.STDOUT,
                                             close_fds=True)
            if output:
                self.log.info("Got output\n{}".format(output))
        except subprocess.CalledProcessError as e:
            self.log.info("Got error output\n{}".format(e.output))
            raise

    def _write_string_args(self, filename):
        # writes string_args to a file, which are read line by line
        with open(filename, 'w') as f:
            f.write('\n'.join(map(str, self.string_args)))

    def _write_args(self, input_filename):
        # serialize args to file
        if self._pass_op_args():
            with open(input_filename, 'wb') as f:
                arg_dict = ({'args': self.op_args, 'kwargs': self.op_kwargs})
                if self.use_dill:
                    dill.dump(arg_dict, f)
                else:
                    pickle.dump(arg_dict, f)

    def _read_result(self, output_filename):
        if os.stat(output_filename).st_size == 0:
            return None
        with open(output_filename, 'rb') as f:
            try:
                if self.use_dill:
                    return dill.load(f)
                else:
                    return pickle.load(f)
            except ValueError:
                self.log.error("Error deserializing result. "
                               "Note that result deserialization "
                               "is not supported across major Python versions.")
                raise

    def _write_script(self, script_filename):
        with open(script_filename, 'w') as f:
            python_code = self._generate_python_code()
            self.log.debug('Writing code to file\n{}'.format(python_code))
            f.write(python_code)

    def _generate_virtualenv_cmd(self, tmp_dir):
        cmd = ['virtualenv', tmp_dir]
        if self.system_site_packages:
            cmd.append('--system-site-packages')
        if self.python_version is not None:
            cmd.append('--python=python{}'.format(self.python_version))
        return cmd

    def _generate_pip_install_cmd(self, tmp_dir):
        if len(self.requirements) == 0:
            return []
        else:
            # direct path alleviates need to activate
            cmd = ['{}/bin/pip'.format(tmp_dir), 'install']
            return cmd + self.requirements

    def _generate_python_cmd(self, tmp_dir, script_filename,
                             input_filename, output_filename, string_args_filename):
        # direct path alleviates need to activate
        return ['{}/bin/python'.format(tmp_dir), script_filename,
                input_filename, output_filename, string_args_filename]

    def _generate_python_code(self):
        if self.use_dill:
            pickling_library = 'dill'
        else:
            pickling_library = 'pickle'
        fn = self.python_callable
        # dont try to read pickle if we didnt pass anything
        if self._pass_op_args():
            load_args_line = 'with open(sys.argv[1], "rb") as f: arg_dict = {}.load(f)'\
                .format(pickling_library)
        else:
            load_args_line = 'arg_dict = {"args": [], "kwargs": {}}'

        # no indents in original code so we can accept
        # any type of indents in the original function
        # we deserialize args, call function, serialize result if necessary
        return dedent("""\
        import {pickling_library}
        import sys
        {load_args_code}
        args = arg_dict["args"]
        kwargs = arg_dict["kwargs"]
        with open(sys.argv[3], 'r') as f:
            virtualenv_string_args = list(map(lambda x: x.strip(), list(f)))
        {python_callable_lines}
        res = {python_callable_name}(*args, **kwargs)
        with open(sys.argv[2], 'wb') as f:
            res is not None and {pickling_library}.dump(res, f)
        """).format(load_args_code=load_args_line,
                    python_callable_lines=dedent(inspect.getsource(fn)),
                    python_callable_name=fn.__name__,
                    pickling_library=pickling_library)

        self.log.info("Done.")
