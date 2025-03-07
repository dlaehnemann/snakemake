__author__ = "Johannes Köster"
__copyright__ = "Copyright 2022, Johannes Köster"
__email__ = "johannes.koester@uni-due.de"
__license__ = "MIT"

import re
import os
import sys
from collections import OrderedDict
from itertools import filterfalse, chain
from functools import partial
import copy
from pathlib import Path
from snakemake.interfaces import WorkflowExecutorInterface


from snakemake.logging import logger, format_resources
from snakemake.rules import Rule, Ruleorder, RuleProxy
from snakemake.exceptions import (
    CreateCondaEnvironmentException,
    RuleException,
    CreateRuleException,
    UnknownRuleException,
    NoRulesException,
    WorkflowError,
)
from snakemake.shell import shell
from snakemake.dag import DAG
from snakemake.scheduler import JobScheduler
from snakemake.parser import parse
import snakemake.io
from snakemake.io import (
    protected,
    temp,
    temporary,
    ancient,
    directory,
    expand,
    dynamic,
    glob_wildcards,
    flag,
    not_iterable,
    touch,
    unpack,
    local,
    pipe,
    service,
    repeat,
    report,
    multiext,
    ensure,
    IOFile,
    sourcecache_entry,
)

from snakemake.persistence import Persistence
from snakemake.utils import update_config
from snakemake.script import script
from snakemake.notebook import notebook
from snakemake.wrapper import wrapper
from snakemake.cwl import cwl
from snakemake.template_rendering import render_template


import snakemake.wrapper
from snakemake.common import (
    Mode,
    ON_WINDOWS,
    is_local_file,
    Rules,
    Scatter,
    Gather,
    smart_join,
    NOTHING_TO_BE_DONE_MSG,
)
from snakemake.utils import simplify_path
from snakemake.checkpoints import Checkpoints
from snakemake.resources import DefaultResources, ResourceScopes
from snakemake.caching.local import OutputFileCache as LocalOutputFileCache
from snakemake.caching.remote import OutputFileCache as RemoteOutputFileCache
from snakemake.modules import ModuleInfo, WorkflowModifier, get_name_modifier_func
from snakemake.ruleinfo import InOutput, RuleInfo
from snakemake.sourcecache import (
    LocalSourceFile,
    SourceCache,
    infer_source_file,
)
from snakemake.deployment.conda import Conda
from snakemake import sourcecache


class Workflow(WorkflowExecutorInterface):
    def __init__(
        self,
        snakefile=None,
        rerun_triggers=None,
        jobscript=None,
        overwrite_shellcmd=None,
        overwrite_config=None,
        overwrite_workdir=None,
        overwrite_configfiles=None,
        overwrite_clusterconfig=None,
        overwrite_threads=None,
        overwrite_scatter=None,
        overwrite_groups=None,
        overwrite_resources=None,
        overwrite_resource_scopes=None,
        group_components=None,
        config_args=None,
        debug=False,
        verbose=False,
        use_conda=False,
        conda_frontend=None,
        conda_prefix=None,
        use_singularity=False,
        use_env_modules=False,
        singularity_prefix=None,
        singularity_args="",
        shadow_prefix=None,
        scheduler_type="ilp",
        scheduler_ilp_solver=None,
        mode=Mode.default,
        wrapper_prefix=None,
        printshellcmds=False,
        restart_times=None,
        attempt=1,
        default_remote_provider=None,
        default_remote_prefix="",
        run_local=True,
        assume_shared_fs=True,
        default_resources=None,
        cache=None,
        nodes=1,
        cores=1,
        resources=None,
        conda_cleanup_pkgs=None,
        edit_notebook=False,
        envvars=None,
        max_inventory_wait_time=20,
        conda_not_block_search_path_envvars=False,
        execute_subworkflows=True,
        scheduler_solver_path=None,
        conda_base_path=None,
        check_envvars=True,
        max_threads=None,
        all_temp=False,
        local_groupid="local",
        keep_metadata=True,
        latency_wait=3,
        cleanup_scripts=True,
        immediate_submit=False,
    ):
        """
        Create the controller.
        """

        self.global_resources = dict() if resources is None else resources
        self.global_resources["_cores"] = cores
        self.global_resources["_nodes"] = nodes

        self._rerun_triggers = (
            frozenset(rerun_triggers) if rerun_triggers is not None else frozenset()
        )
        self._rules = OrderedDict()
        self.default_target = None
        self._workdir = None
        self.overwrite_workdir = overwrite_workdir
        self._workdir_init = os.path.abspath(os.curdir)
        self._cleanup_scripts = cleanup_scripts
        self._ruleorder = Ruleorder()
        self._localrules = set()
        self._linemaps = dict()
        self.rule_count = 0
        self.basedir = os.path.dirname(snakefile)
        self._main_snakefile = os.path.abspath(snakefile)
        self.included = []
        self.included_stack = []
        self._jobscript = jobscript
        self._persistence = None
        self._subworkflows = dict()
        self.overwrite_shellcmd = overwrite_shellcmd
        self.overwrite_config = overwrite_config or dict()
        self._overwrite_configfiles = overwrite_configfiles
        self.overwrite_clusterconfig = overwrite_clusterconfig or dict()
        self._overwrite_threads = overwrite_threads or dict()
        self._overwrite_resources = overwrite_resources or dict()
        self._config_args = config_args
        self._immediate_submit = immediate_submit
        self._onsuccess = lambda log: None
        self._onerror = lambda log: None
        self._onstart = lambda log: None
        self._debug = debug
        self._verbose = verbose
        self._rulecount = 0
        self._use_conda = use_conda
        self._conda_frontend = conda_frontend
        self._conda_prefix = conda_prefix
        self._use_singularity = use_singularity
        self._use_env_modules = use_env_modules
        self.singularity_prefix = singularity_prefix
        self._singularity_args = singularity_args
        self._shadow_prefix = shadow_prefix
        self._scheduler_type = scheduler_type
        self.scheduler_ilp_solver = scheduler_ilp_solver
        self.global_container_img = None
        self.global_is_containerized = False
        self.mode = mode
        self._wrapper_prefix = wrapper_prefix
        self._printshellcmds = printshellcmds
        self.restart_times = restart_times
        self.attempt = attempt
        self.default_remote_provider = default_remote_provider
        self._default_remote_prefix = default_remote_prefix
        self.configfiles = (
            [] if overwrite_configfiles is None else list(overwrite_configfiles)
        )
        self.run_local = run_local
        self.assume_shared_fs = assume_shared_fs
        self.report_text = None
        self.conda_cleanup_pkgs = conda_cleanup_pkgs
        self._edit_notebook = edit_notebook
        # environment variables to pass to jobs
        # These are defined via the "envvars:" syntax in the Snakefile itself
        self._envvars = set()
        self.overwrite_groups = overwrite_groups or dict()
        self.group_components = group_components or dict()
        self._scatter = dict(overwrite_scatter or dict())
        self._overwrite_scatter = overwrite_scatter or dict()
        self._overwrite_resource_scopes = overwrite_resource_scopes or dict()
        self._resource_scopes = ResourceScopes.defaults()
        self._resource_scopes.update(self.overwrite_resource_scopes)
        self._conda_not_block_search_path_envvars = conda_not_block_search_path_envvars
        self._execute_subworkflows = execute_subworkflows
        self.modules = dict()
        self._sourcecache = SourceCache()
        self.scheduler_solver_path = scheduler_solver_path
        self._conda_base_path = conda_base_path
        self.check_envvars = check_envvars
        self._max_threads = max_threads
        self.all_temp = all_temp
        self._scheduler = None
        self._local_groupid = local_groupid
        self._keep_metadata = keep_metadata
        self._latency_wait = latency_wait

        _globals = globals()
        _globals["workflow"] = self
        _globals["cluster_config"] = copy.deepcopy(self.overwrite_clusterconfig)
        _globals["rules"] = Rules()
        _globals["checkpoints"] = Checkpoints()
        _globals["scatter"] = Scatter()
        _globals["gather"] = Gather()
        _globals["github"] = sourcecache.GithubFile
        _globals["gitlab"] = sourcecache.GitlabFile
        _globals["gitfile"] = sourcecache.LocalGitFile

        self.vanilla_globals = dict(_globals)
        self.modifier_stack = [WorkflowModifier(self, globals=_globals)]

        self.enable_cache = False
        if cache is not None:
            self.enable_cache = True
            self.cache_rules = {rulename: "all" for rulename in cache}
            if self.default_remote_provider is not None:
                self._output_file_cache = RemoteOutputFileCache(
                    self.default_remote_provider
                )
            else:
                self._output_file_cache = LocalOutputFileCache()
        else:
            self._output_file_cache = None
            self.cache_rules = dict()

        if default_resources is not None:
            self._default_resources = default_resources
        else:
            # only _cores, _nodes, and _tmpdir
            self._default_resources = DefaultResources(mode="bare")

        self.iocache = snakemake.io.IOCache(max_inventory_wait_time)

        self.globals["config"] = copy.deepcopy(self.overwrite_config)

        if envvars is not None:
            self.register_envvars(*envvars)

    @property
    def default_remote_prefix(self):
        return self._default_remote_prefix

    @property
    def immediate_submit(self):
        return self._immediate_submit

    @property
    def scheduler(self):
        return self._scheduler

    @scheduler.setter
    def scheduler(self, scheduler):
        self._scheduler = scheduler

    @property
    def envvars(self):
        return self._envvars

    @property
    def jobscript(self):
        return self._jobscript

    @property
    def verbose(self):
        return self._verbose

    @property
    def sourcecache(self):
        return self._sourcecache

    @property
    def edit_notebook(self):
        return self._edit_notebook

    @property
    def cleanup_scripts(self):
        return self._cleanup_scripts

    @property
    def debug(self):
        return self._debug

    @property
    def use_env_modules(self):
        return self._use_env_modules

    @property
    def use_singularity(self):
        return self._use_singularity

    @property
    def use_conda(self):
        return self._use_conda

    @property
    def workdir_init(self):
        return self._workdir_init

    @property
    def linemaps(self):
        return self._linemaps

    @property
    def persistence(self):
        return self._persistence

    @property
    def main_snakefile(self):
        return self._main_snakefile

    @property
    def output_file_cache(self):
        return self._output_file_cache

    @property
    def resource_scopes(self):
        return self._resource_scopes

    @property
    def overwrite_resource_scopes(self):
        return self._overwrite_resource_scopes

    @property
    def default_resources(self):
        return self._default_resources

    @property
    def scheduler_type(self):
        return self._scheduler_type

    @property
    def printshellcmds(self):
        return self._printshellcmds

    @property
    def config_args(self):
        return self._config_args

    @property
    def overwrite_configfiles(self):
        return self._overwrite_configfiles

    @property
    def conda_not_block_search_path_envvars(self):
        return self._conda_not_block_search_path_envvars

    @property
    def local_groupid(self):
        return self._local_groupid

    @property
    def overwrite_scatter(self):
        return self._overwrite_scatter

    @property
    def overwrite_threads(self):
        return self._overwrite_threads

    @property
    def wrapper_prefix(self):
        return self._wrapper_prefix

    @property
    def keep_metadata(self):
        return self._keep_metadata

    @property
    def max_threads(self):
        return self._max_threads

    @property
    def execute_subworkflows(self):
        return self._execute_subworkflows

    @property
    def singularity_args(self):
        return self._singularity_args

    @property
    def conda_prefix(self):
        return self._conda_prefix

    @property
    def conda_frontend(self):
        return self._conda_frontend

    @property
    def shadow_prefix(self):
        return self._shadow_prefix

    @property
    def rerun_triggers(self):
        return self._rerun_triggers

    @property
    def latency_wait(self):
        return self._latency_wait

    @property
    def overwrite_resources(self):
        return self._overwrite_resources

    @property
    def conda_base_path(self):
        if self._conda_base_path:
            return self._conda_base_path
        if self.use_conda:
            try:
                return Conda().prefix_path
            except CreateCondaEnvironmentException as e:
                # Return no preset conda base path now and report error later in jobs.
                return None
        else:
            return None

    @property
    def modifier(self):
        return self.modifier_stack[-1]

    @property
    def wildcard_constraints(self):
        return self.modifier.wildcard_constraints

    @property
    def globals(self):
        return self.modifier.globals

    def lint(self, json=False):
        from snakemake.linting.rules import RuleLinter
        from snakemake.linting.snakefiles import SnakefileLinter

        json_snakefile_lints, snakefile_linted = SnakefileLinter(
            self, self.included
        ).lint(json=json)
        json_rule_lints, rules_linted = RuleLinter(self, self.rules).lint(json=json)

        linted = snakefile_linted or rules_linted

        if json:
            import json

            print(
                json.dumps(
                    {"snakefiles": json_snakefile_lints, "rules": json_rule_lints},
                    indent=2,
                )
            )
        else:
            if not linted:
                logger.info("Congratulations, your workflow is in a good condition!")
        return linted

    def get_cache_mode(self, rule: Rule):
        return self.cache_rules.get(rule.name)

    @property
    def subworkflows(self):
        return self._subworkflows.values()

    @property
    def rules(self):
        return self._rules.values()

    @property
    def cores(self):
        if self._cores is None:
            raise WorkflowError(
                "Workflow requires a total number of cores to be defined (e.g. because a "
                "rule defines its number of threads as a fraction of a total number of cores). "
                "Please set it with --cores N with N being the desired number of cores. "
                "Consider to use this in combination with --max-threads to avoid "
                "jobs with too many threads for your setup. Also make sure to perform "
                "a dryrun first."
            )
        return self._cores

    @property
    def _cores(self):
        return self.global_resources["_cores"]

    @property
    def nodes(self):
        return self.global_resources["_nodes"]

    @property
    def concrete_files(self):
        return (
            file
            for rule in self.rules
            for file in chain(rule.input, rule.output)
            if not callable(file) and not file.contains_wildcard()
        )

    def check(self):
        for clause in self._ruleorder:
            for rulename in clause:
                if not self.is_rule(rulename):
                    raise UnknownRuleException(
                        rulename, prefix="Error in ruleorder definition."
                    )

    def add_rule(
        self,
        name=None,
        lineno=None,
        snakefile=None,
        checkpoint=False,
        allow_overwrite=False,
    ):
        """
        Add a rule.
        """
        is_overwrite = self.is_rule(name)
        if not allow_overwrite and is_overwrite:
            raise CreateRuleException(
                f"The name {name} is already used by another rule",
                lineno=lineno,
                snakefile=snakefile,
            )
        rule = Rule(name, self, lineno=lineno, snakefile=snakefile)
        self._rules[rule.name] = rule
        self.modifier.rules.add(rule)
        if not is_overwrite:
            self.rule_count += 1
        if not self.default_target:
            self.default_target = rule.name
        return name

    def is_rule(self, name):
        """
        Return True if name is the name of a rule.

        Arguments
        name -- a name
        """
        return name in self._rules

    def get_rule(self, name):
        """
        Get rule by name.

        Arguments
        name -- the name of the rule
        """
        if not self._rules:
            raise NoRulesException()
        if not name in self._rules:
            raise UnknownRuleException(name)
        return self._rules[name]

    def list_rules(self, only_targets=False):
        rules = self.rules
        if only_targets:
            rules = filterfalse(Rule.has_wildcards, rules)
        for rule in sorted(rules, key=lambda r: r.name):
            logger.rule_info(name=rule.name, docstring=rule.docstring)

    def list_resources(self):
        for resource in set(
            resource for rule in self.rules for resource in rule.resources
        ):
            if resource not in "_cores _nodes".split():
                logger.info(resource)

    def is_local(self, rule):
        return rule.group is None and (
            rule.name in self._localrules or rule.norun or rule.is_template_engine
        )

    def check_localrules(self):
        undefined = self._localrules - set(rule.name for rule in self.rules)
        if undefined:
            logger.warning(
                "localrules directive specifies rules that are not "
                "present in the Snakefile:\n{}\n".format(
                    "\n".join(map("\t{}".format, undefined))
                )
            )

    def inputfile(self, path):
        """Mark file as being an input file of the workflow.

        This also means that eventual --default-remote-provider/prefix settings
        will be applied to this file. The file is returned as _IOFile object,
        such that it can e.g. be transparently opened with _IOFile.open().
        """
        if isinstance(path, Path):
            path = str(path)
        if self.default_remote_provider is not None:
            path = self.modifier.modify_path(path)
        return IOFile(path)

    def execute(
        self,
        targets=None,
        target_jobs=None,
        dryrun=False,
        generate_unit_tests=None,
        touch=False,
        scheduler_type=None,
        scheduler_ilp_solver=None,
        local_cores=1,
        forcetargets=False,
        forceall=False,
        forcerun=None,
        until=[],
        omit_from=[],
        prioritytargets=None,
        quiet=False,
        keepgoing=False,
        printshellcmds=False,
        printreason=False,
        printdag=False,
        slurm=None,
        slurm_jobstep=None,
        cluster=None,
        cluster_sync=None,
        jobname=None,
        ignore_ambiguity=False,
        printrulegraph=False,
        printfilegraph=False,
        printd3dag=False,
        drmaa=None,
        drmaa_log_dir=None,
        kubernetes=None,
        k8s_cpu_scalar=1.0,
        flux=None,
        tibanna=None,
        tibanna_sfn=None,
        az_batch=False,
        az_batch_enable_autoscale=False,
        az_batch_account_url=None,
        google_lifesciences=None,
        google_lifesciences_regions=None,
        google_lifesciences_location=None,
        google_lifesciences_cache=False,
        tes=None,
        precommand="",
        preemption_default=None,
        preemptible_rules=None,
        tibanna_config=False,
        container_image=None,
        stats=None,
        force_incomplete=False,
        ignore_incomplete=False,
        list_version_changes=False,
        list_code_changes=False,
        list_input_changes=False,
        list_params_changes=False,
        list_untracked=False,
        list_conda_envs=False,
        summary=False,
        archive=None,
        delete_all_output=False,
        delete_temp_output=False,
        detailed_summary=False,
        wait_for_files=None,
        nolock=False,
        unlock=False,
        notemp=False,
        nodeps=False,
        cleanup_metadata=None,
        conda_cleanup_envs=False,
        cleanup_containers=False,
        cleanup_shadow=False,
        subsnakemake=None,
        updated_files=None,
        keep_target_files=False,
        keep_shadow=False,
        keep_remote_local=False,
        allowed_rules=None,
        max_jobs_per_second=None,
        max_status_checks_per_second=None,
        greediness=1.0,
        no_hooks=False,
        force_use_threads=False,
        conda_create_envs_only=False,
        cluster_status=None,
        cluster_cancel=None,
        cluster_cancel_nargs=None,
        cluster_sidecar=None,
        report=None,
        report_stylesheet=None,
        export_cwl=False,
        batch=None,
        keepincomplete=False,
        containerize=False,
    ):
        self.check_localrules()

        def rules(items):
            return map(self._rules.__getitem__, filter(self.is_rule, items))

        if keep_target_files:

            def files(items):
                return filterfalse(self.is_rule, items)

        else:

            def files(items):
                relpath = (
                    lambda f: f
                    if os.path.isabs(f) or f.startswith("root://")
                    else os.path.relpath(f)
                )
                return map(relpath, filterfalse(self.is_rule, items))

        if not targets and not target_jobs:
            targets = (
                [self.default_target] if self.default_target is not None else list()
            )

        if prioritytargets is None:
            prioritytargets = list()
        if forcerun is None:
            forcerun = list()
        if until is None:
            until = list()
        if omit_from is None:
            omit_from = list()

        priorityrules = set(rules(prioritytargets))
        priorityfiles = set(files(prioritytargets))
        forcerules = set(rules(forcerun))
        forcefiles = set(files(forcerun))
        untilrules = set(rules(until))
        untilfiles = set(files(until))
        omitrules = set(rules(omit_from))
        omitfiles = set(files(omit_from))
        targetrules = set(
            chain(
                rules(targets),
                filterfalse(Rule.has_wildcards, priorityrules),
                filterfalse(Rule.has_wildcards, forcerules),
                filterfalse(Rule.has_wildcards, untilrules),
            )
        )
        targetfiles = set(chain(files(targets), priorityfiles, forcefiles, untilfiles))

        if ON_WINDOWS:
            targetfiles = set(tf.replace(os.sep, os.altsep) for tf in targetfiles)

        if forcetargets:
            forcefiles.update(targetfiles)
            forcerules.update(targetrules)

        rules = self.rules
        if allowed_rules:
            allowed_rules = set(allowed_rules)
            rules = [rule for rule in rules if rule.name in allowed_rules]

        if wait_for_files is not None:
            try:
                snakemake.io.wait_for_files(
                    wait_for_files, latency_wait=self.latency_wait
                )
            except IOError as e:
                logger.error(str(e))
                return False

        dag = DAG(
            self,
            rules,
            dryrun=dryrun,
            targetfiles=targetfiles,
            targetrules=targetrules,
            target_jobs_def=target_jobs,
            # when cleaning up conda or containers, we should enforce all possible jobs
            # since their envs shall not be deleted
            forceall=forceall or conda_cleanup_envs or cleanup_containers,
            forcefiles=forcefiles,
            forcerules=forcerules,
            priorityfiles=priorityfiles,
            priorityrules=priorityrules,
            untilfiles=untilfiles,
            untilrules=untilrules,
            omitfiles=omitfiles,
            omitrules=omitrules,
            ignore_ambiguity=ignore_ambiguity,
            force_incomplete=force_incomplete,
            ignore_incomplete=ignore_incomplete
            or printdag
            or printrulegraph
            or printfilegraph,
            notemp=notemp,
            keep_remote_local=keep_remote_local,
            batch=batch,
        )

        self._persistence = Persistence(
            nolock=nolock,
            dag=dag,
            conda_prefix=self.conda_prefix,
            singularity_prefix=self.singularity_prefix,
            shadow_prefix=self.shadow_prefix,
            warn_only=dryrun
            or printrulegraph
            or printfilegraph
            or printdag
            or summary
            or detailed_summary
            or archive
            or list_version_changes
            or list_code_changes
            or list_input_changes
            or list_params_changes
            or list_untracked
            or delete_all_output
            or delete_temp_output,
        )

        if self.mode in [Mode.subprocess, Mode.cluster]:
            self.persistence.deactivate_cache()

        if cleanup_metadata:
            failed = []
            for f in cleanup_metadata:
                success = self.persistence.cleanup_metadata(f)
                if not success:
                    failed.append(f)
            if failed:
                logger.warning(
                    "Failed to clean up metadata for the following files because the metadata was not present.\n"
                    "If this is expected, there is nothing to do.\nOtherwise, the reason might be file system latency "
                    "or still running jobs.\nConsider running metadata cleanup again.\nFiles:\n"
                    + "\n".join(failed)
                )
            return True

        if unlock:
            try:
                self.persistence.cleanup_locks()
                logger.info("Unlocking working directory.")
                return True
            except IOError:
                logger.error(
                    "Error: Unlocking the directory {} failed. Maybe "
                    "you don't have the permissions?"
                )
                return False

        logger.info("Building DAG of jobs...")
        dag.init()
        dag.update_checkpoint_dependencies()
        dag.check_dynamic()

        self.persistence.lock()

        if cleanup_shadow:
            self.persistence.cleanup_shadow()
            return True

        if containerize:
            from snakemake.deployment.containerize import containerize

            containerize(self, dag)
            return True

        if (
            self.subworkflows
            and self.execute_subworkflows
            and not printdag
            and not printrulegraph
            and not printfilegraph
        ):
            # backup globals
            globals_backup = dict(self.globals)
            # execute subworkflows
            for subworkflow in self.subworkflows:
                subworkflow_targets = subworkflow.targets(dag)
                logger.debug(
                    "Files requested from subworkflow:\n    {}".format(
                        "\n    ".join(subworkflow_targets)
                    )
                )
                updated = list()
                if subworkflow_targets:
                    logger.info(f"Executing subworkflow {subworkflow.name}.")
                    if not subsnakemake(
                        subworkflow.snakefile,
                        workdir=subworkflow.workdir,
                        targets=subworkflow_targets,
                        cores=self._cores,
                        nodes=self.nodes,
                        resources=self.global_resources,
                        configfiles=[subworkflow.configfile]
                        if subworkflow.configfile
                        else None,
                        updated_files=updated,
                        rerun_triggers=self.rerun_triggers,
                    ):
                        return False
                    dag.updated_subworkflow_files.update(
                        subworkflow.target(f) for f in updated
                    )
                else:
                    logger.info(
                        f"Subworkflow {subworkflow.name}: {NOTHING_TO_BE_DONE_MSG}"
                    )
            if self.subworkflows:
                logger.info("Executing main workflow.")
            # rescue globals
            self.globals.update(globals_backup)

        dag.postprocess(update_needrun=False)
        if not dryrun:
            # deactivate IOCache such that from now on we always get updated
            # size, existence and mtime information
            # ATTENTION: this may never be removed without really good reason.
            # Otherwise weird things may happen.
            self.iocache.deactivate()
            # clear and deactivate persistence cache, from now on we want to see updates
            self.persistence.deactivate_cache()

        if nodeps:
            missing_input = [
                f
                for job in dag.targetjobs
                for f in job.input
                if dag.needrun(job) and not os.path.exists(f)
            ]
            if missing_input:
                logger.error(
                    "Dependency resolution disabled (--nodeps) "
                    "but missing input "
                    "files detected. If this happens on a cluster, please make sure "
                    "that you handle the dependencies yourself or turn off "
                    "--immediate-submit. Missing input files:\n{}".format(
                        "\n".join(missing_input)
                    )
                )
                return False

        if self.immediate_submit and any(dag.checkpoint_jobs):
            logger.error(
                "Immediate submit mode (--immediate-submit) may not be used for workflows "
                "with checkpoint jobs, as the dependencies cannot be determined before "
                "execution in such cases."
            )
            return False

        updated_files.extend(f for job in dag.needrun_jobs() for f in job.output)

        if generate_unit_tests:
            from snakemake import unit_tests

            path = generate_unit_tests
            deploy = []
            if self.use_conda:
                deploy.append("conda")
            if self.use_singularity:
                deploy.append("singularity")
            unit_tests.generate(
                dag, path, deploy, configfiles=self.overwrite_configfiles
            )
            return True
        elif export_cwl:
            from snakemake.cwl import dag_to_cwl
            import json

            with open(export_cwl, "w") as cwl:
                json.dump(dag_to_cwl(dag), cwl, indent=4)
            return True
        elif report:
            from snakemake.report import auto_report

            auto_report(dag, report, stylesheet=report_stylesheet)
            return True
        elif printd3dag:
            dag.d3dag()
            return True
        elif printdag:
            print(dag)
            return True
        elif printrulegraph:
            print(dag.rule_dot())
            return True
        elif printfilegraph:
            print(dag.filegraph_dot())
            return True
        elif summary:
            print("\n".join(dag.summary(detailed=False)))
            return True
        elif detailed_summary:
            print("\n".join(dag.summary(detailed=True)))
            return True
        elif archive:
            dag.archive(archive)
            return True
        elif delete_all_output:
            dag.clean(only_temp=False, dryrun=dryrun)
            return True
        elif delete_temp_output:
            dag.clean(only_temp=True, dryrun=dryrun)
            return True
        elif list_version_changes:
            items = dag.get_outputs_with_changes("version")
            if items:
                print(*items, sep="\n")
            return True
        elif list_code_changes:
            items = dag.get_outputs_with_changes("code")
            if items:
                print(*items, sep="\n")
            return True
        elif list_input_changes:
            items = dag.get_outputs_with_changes("input")
            if items:
                print(*items, sep="\n")
            return True
        elif list_params_changes:
            items = dag.get_outputs_with_changes("params")
            if items:
                print(*items, sep="\n")
            return True
        elif list_untracked:
            dag.list_untracked()
            return True

        if self.use_singularity and self.assume_shared_fs:
            dag.pull_container_imgs(
                dryrun=dryrun or list_conda_envs or cleanup_containers,
                quiet=list_conda_envs,
            )
        if self.use_conda:
            dag.create_conda_envs(
                dryrun=dryrun or list_conda_envs or conda_cleanup_envs,
                quiet=list_conda_envs,
            )
            if conda_create_envs_only:
                return True

        if list_conda_envs:
            print("environment", "container", "location", sep="\t")
            for env in set(job.conda_env for job in dag.jobs):
                if env and not env.is_named:
                    print(
                        env.file.simplify_path(),
                        env.container_img_url or "",
                        simplify_path(env.address),
                        sep="\t",
                    )
            return True

        if conda_cleanup_envs:
            self.persistence.conda_cleanup_envs()
            return True

        if cleanup_containers:
            self.persistence.cleanup_containers()
            return True

        self.scheduler = JobScheduler(
            self,
            dag,
            local_cores=local_cores,
            dryrun=dryrun,
            touch=touch,
            slurm=slurm,
            slurm_jobstep=slurm_jobstep,
            cluster=cluster,
            cluster_status=cluster_status,
            cluster_cancel=cluster_cancel,
            cluster_cancel_nargs=cluster_cancel_nargs,
            cluster_sidecar=cluster_sidecar,
            cluster_config=cluster_config,
            cluster_sync=cluster_sync,
            jobname=jobname,
            max_jobs_per_second=max_jobs_per_second,
            max_status_checks_per_second=max_status_checks_per_second,
            quiet=quiet,
            keepgoing=keepgoing,
            drmaa=drmaa,
            drmaa_log_dir=drmaa_log_dir,
            kubernetes=kubernetes,
            k8s_cpu_scalar=k8s_cpu_scalar,
            flux=flux,
            tibanna=tibanna,
            tibanna_sfn=tibanna_sfn,
            az_batch=az_batch,
            az_batch_enable_autoscale=az_batch_enable_autoscale,
            az_batch_account_url=az_batch_account_url,
            google_lifesciences=google_lifesciences,
            google_lifesciences_regions=google_lifesciences_regions,
            google_lifesciences_location=google_lifesciences_location,
            google_lifesciences_cache=google_lifesciences_cache,
            tes=tes,
            preemption_default=preemption_default,
            preemptible_rules=preemptible_rules,
            precommand=precommand,
            tibanna_config=tibanna_config,
            container_image=container_image,
            printreason=printreason,
            printshellcmds=printshellcmds,
            greediness=greediness,
            force_use_threads=force_use_threads,
            assume_shared_fs=self.assume_shared_fs,
            keepincomplete=keepincomplete,
            scheduler_type=scheduler_type,
            scheduler_ilp_solver=scheduler_ilp_solver,
        )

        if not dryrun:
            if len(dag):
                shell_exec = shell.get_executable()
                if shell_exec is not None:
                    logger.info(f"Using shell: {shell_exec}")
                if cluster or cluster_sync or drmaa:
                    logger.resources_info(f"Provided cluster nodes: {self.nodes}")
                elif kubernetes or tibanna or google_lifesciences:
                    logger.resources_info(f"Provided cloud nodes: {self.nodes}")
                else:
                    if self._cores is not None:
                        warning = (
                            ""
                            if self._cores > 1
                            else " (use --cores to define parallelism)"
                        )
                        logger.resources_info(f"Provided cores: {self._cores}{warning}")
                        logger.resources_info(
                            "Rules claiming more threads will be scaled down."
                        )

                provided_resources = format_resources(self.global_resources)
                if provided_resources:
                    logger.resources_info(f"Provided resources: {provided_resources}")

                if self.run_local and any(rule.group for rule in self.rules):
                    logger.info("Group jobs: inactive (local execution)")

                if not self.use_conda and any(rule.conda_env for rule in self.rules):
                    logger.info("Conda environments: ignored")

                if not self.use_singularity and any(
                    rule.container_img for rule in self.rules
                ):
                    logger.info("Singularity containers: ignored")

                if self.mode == Mode.default:
                    logger.run_info("\n".join(dag.stats()))
            else:
                logger.info(NOTHING_TO_BE_DONE_MSG)
        else:
            # the dryrun case
            if len(dag):
                logger.run_info("\n".join(dag.stats()))
            else:
                logger.info(NOTHING_TO_BE_DONE_MSG)
                return True
            if quiet:
                # in case of dryrun and quiet, just print above info and exit
                return True

        if not dryrun and not no_hooks:
            self._onstart(logger.get_logfile())

        def log_provenance_info():
            provenance_triggered_jobs = [
                job
                for job in dag.needrun_jobs(exclude_finished=False)
                if dag.reason(job).is_provenance_triggered()
            ]
            if provenance_triggered_jobs:
                logger.info(
                    "Some jobs were triggered by provenance information, "
                    "see 'reason' section in the rule displays above.\n"
                    "If you prefer that only modification time is used to "
                    "determine whether a job shall be executed, use the command "
                    "line option '--rerun-triggers mtime' (also see --help).\n"
                    "If you are sure that a change for a certain output file (say, <outfile>) won't "
                    "change the result (e.g. because you just changed the formatting of a script "
                    "or environment definition), you can also wipe its metadata to skip such a trigger via "
                    "'snakemake --cleanup-metadata <outfile>'. "
                )
                logger.info(
                    "Rules with provenance triggered jobs: "
                    + ",".join(
                        sorted(set(job.rule.name for job in provenance_triggered_jobs))
                    )
                )
                logger.info("")

        has_checkpoint_jobs = any(dag.checkpoint_jobs)

        try:
            success = self.scheduler.schedule()
        except Exception as e:
            if dryrun:
                log_provenance_info()
            raise e

        if not self.immediate_submit and not dryrun and self.mode == Mode.default:
            dag.cleanup_workdir()

        if success:
            if dryrun:
                if len(dag):
                    logger.run_info("\n".join(dag.stats()))
                    dag.print_reasons()
                    log_provenance_info()
                logger.info("")
                logger.info(
                    "This was a dry-run (flag -n). The order of jobs "
                    "does not reflect the order of execution."
                )
                if has_checkpoint_jobs:
                    logger.info(
                        "The run involves checkpoint jobs, "
                        "which will result in alteration of the DAG of "
                        "jobs (e.g. adding more jobs) after their completion."
                    )
            else:
                if stats:
                    self.scheduler.stats.to_json(stats)
                logger.logfile_hint()
            if not dryrun and not no_hooks:
                self._onsuccess(logger.get_logfile())
            return True
        else:
            if not dryrun and not no_hooks:
                self._onerror(logger.get_logfile())
            logger.logfile_hint()
            return False

    @property
    def current_basedir(self):
        """Basedir of currently parsed Snakefile."""
        assert self.included_stack
        snakefile = self.included_stack[-1]
        basedir = snakefile.get_basedir()
        if isinstance(basedir, LocalSourceFile):
            return basedir.abspath()
        else:
            return basedir

    def source_path(self, rel_path):
        """Return path to source file from work dir derived from given path relative to snakefile"""
        # TODO download to disk (use source cache) in case of remote file
        import inspect

        frame = inspect.currentframe().f_back
        calling_file = frame.f_code.co_filename

        if (
            self.included_stack
            and calling_file == self.included_stack[-1].get_path_or_uri()
        ):
            # called from current snakefile, we can try to keep the original source
            # file annotation
            # This will only work if the method is evaluated during parsing mode.
            # Otherwise, the stack can be empty already.
            path = self.current_basedir.join(rel_path)
            orig_path = path.get_path_or_uri()
        else:
            # heuristically determine path
            calling_dir = os.path.dirname(calling_file)
            path = smart_join(calling_dir, rel_path)
            orig_path = path

        return sourcecache_entry(
            self.sourcecache.get_path(infer_source_file(path)), orig_path
        )

    @property
    def snakefile(self):
        import inspect

        frame = inspect.currentframe().f_back
        return frame.f_code.co_filename

    def register_envvars(self, *envvars):
        """
        Register environment variables that shall be passed to jobs.
        If used multiple times, union is taken.
        """
        invalid_envvars = [
            envvar
            for envvar in envvars
            if re.match(r"^\w+$", envvar, flags=re.ASCII) is None
        ]
        if invalid_envvars:
            raise WorkflowError(
                f"Invalid environment variables requested: {', '.join(map(repr, invalid_envvars))}. "
                "Environment variable names may only contain alphanumeric characters and the underscore. "
            )
        undefined = set(var for var in envvars if var not in os.environ)
        if self.check_envvars and undefined:
            raise WorkflowError(
                "The following environment variables are requested by the workflow but undefined. "
                "Please make sure that they are correctly defined before running Snakemake:\n"
                "{}".format("\n".join(undefined))
            )
        self.envvars.update(envvars)

    def include(
        self,
        snakefile,
        overwrite_default_target=False,
        print_compilation=False,
        overwrite_shellcmd=None,
    ):
        """
        Include a snakefile.
        """
        basedir = self.current_basedir if self.included_stack else None
        snakefile = infer_source_file(snakefile, basedir)

        if not self.modifier.allow_rule_overwrite and snakefile in self.included:
            logger.info(f"Multiple includes of {snakefile} ignored")
            return
        self.included.append(snakefile)
        self.included_stack.append(snakefile)

        default_target = self.default_target
        code, linemap, rulecount = parse(
            snakefile,
            self,
            overwrite_shellcmd=self.overwrite_shellcmd,
            rulecount=self._rulecount,
        )
        self._rulecount = rulecount

        if print_compilation:
            print(code)

        if isinstance(snakefile, LocalSourceFile):
            # insert the current directory into sys.path
            # this allows to import modules from the workflow directory
            sys.path.insert(0, snakefile.get_basedir().get_path_or_uri())

        self.linemaps[snakefile.get_path_or_uri()] = linemap

        exec(compile(code, snakefile.get_path_or_uri(), "exec"), self.globals)

        if not overwrite_default_target:
            self.default_target = default_target
        self.included_stack.pop()

    def onstart(self, func):
        """Register onstart function."""
        self._onstart = func

    def onsuccess(self, func):
        """Register onsuccess function."""
        self._onsuccess = func

    def onerror(self, func):
        """Register onerror function."""
        self._onerror = func

    def global_wildcard_constraints(self, **content):
        """Register global wildcard constraints."""
        self.modifier.wildcard_constraints.update(content)
        # update all rules so far
        for rule in self.modifier.rules:
            rule.update_wildcard_constraints()

    def scattergather(self, **content):
        """Register scattergather defaults."""
        self._scatter.update(content)
        self._scatter.update(self.overwrite_scatter)

        # add corresponding wildcard constraint
        self.global_wildcard_constraints(scatteritem=r"\d+-of-\d+")

        def func(key, *args, **wildcards):
            n = self._scatter[key]
            return expand(
                *args,
                scatteritem=map(f"{{}}-of-{n}".format, range(1, n + 1)),
                **wildcards,
            )

        for key in content:
            setattr(self.globals["scatter"], key, partial(func, key))
            setattr(self.globals["gather"], key, partial(func, key))

    def resourcescope(self, **content):
        """Register resource scope defaults"""
        self.resource_scopes.update(content)
        self.resource_scopes.update(self.overwrite_resource_scopes)

    def workdir(self, workdir):
        """Register workdir."""
        if self.overwrite_workdir is None:
            os.makedirs(workdir, exist_ok=True)
            self._workdir = workdir
            os.chdir(workdir)

    def configfile(self, fp):
        """Update the global config with data from the given file."""
        if not self.modifier.skip_configfile:
            if os.path.exists(fp):
                self.configfiles.append(fp)
                c = snakemake.io.load_configfile(fp)
                update_config(self.config, c)
                if self.overwrite_config:
                    logger.info(
                        "Config file {} is extended by additional config specified via the command line.".format(
                            fp
                        )
                    )
                    update_config(self.config, self.overwrite_config)
            elif not self.overwrite_configfiles:
                fp_full = os.path.abspath(fp)
                raise WorkflowError(
                    f"Workflow defines configfile {fp} but it is not present or accessible (full checked path: {fp_full})."
                )
            else:
                # CLI configfiles have been specified, do not throw an error but update with their values
                update_config(self.config, self.overwrite_config)

    def set_pepfile(self, path):
        try:
            import peppy
        except ImportError:
            raise WorkflowError("For PEP support, please install peppy.")

        self.pepfile = path
        self.globals["pep"] = peppy.Project(self.pepfile)

    def pepschema(self, schema):
        try:
            import eido
        except ImportError:
            raise WorkflowError("For PEP schema support, please install eido.")

        if is_local_file(schema) and not os.path.isabs(schema):
            # schema is relative to current Snakefile
            schema = self.current_basedir.join(schema).get_path_or_uri()
        if self.pepfile is None:
            raise WorkflowError("Please specify a PEP with the pepfile directive.")
        eido.validate_project(project=self.globals["pep"], schema=schema)

    def report(self, path):
        """Define a global report description in .rst format."""
        if not self.modifier.skip_global_report_caption:
            self.report_text = self.current_basedir.join(path)

    @property
    def config(self):
        return self.globals["config"]

    def ruleorder(self, *rulenames):
        self._ruleorder.add(*map(self.modifier.modify_rulename, rulenames))

    def subworkflow(self, name, snakefile=None, workdir=None, configfile=None):
        # Take absolute path of config file, because it is relative to current
        # workdir, which could be changed for the subworkflow.
        if configfile:
            configfile = os.path.abspath(configfile)
        sw = Subworkflow(self, name, snakefile, workdir, configfile)
        self._subworkflows[name] = sw
        self.globals[name] = sw.target

    def localrules(self, *rulenames):
        self._localrules.update(rulenames)

    def rule(self, name=None, lineno=None, snakefile=None, checkpoint=False):
        # choose a name for an unnamed rule
        if name is None:
            name = str(len(self._rules) + 1)

        if self.modifier.skip_rule(name):

            def decorate(ruleinfo):
                # do nothing, ignore rule
                return ruleinfo.func

            return decorate

        # Optionally let the modifier change the rulename.
        orig_name = name
        name = self.modifier.modify_rulename(name)

        name = self.add_rule(
            name,
            lineno,
            snakefile,
            checkpoint,
            allow_overwrite=self.modifier.allow_rule_overwrite,
        )
        rule = self.get_rule(name)
        rule.is_checkpoint = checkpoint
        rule.module_globals = self.modifier.globals

        def decorate(ruleinfo):
            nonlocal name

            # If requested, modify ruleinfo via the modifier.
            ruleinfo.apply_modifier(self.modifier)

            if ruleinfo.wildcard_constraints:
                rule.set_wildcard_constraints(
                    *ruleinfo.wildcard_constraints[0],
                    **ruleinfo.wildcard_constraints[1],
                )
            if ruleinfo.name:
                rule.name = ruleinfo.name
                del self._rules[name]
                self._rules[ruleinfo.name] = rule
                name = rule.name
            if ruleinfo.input:
                rule.input_modifier = ruleinfo.input.modifier
                rule.set_input(*ruleinfo.input.paths, **ruleinfo.input.kwpaths)
            if ruleinfo.output:
                rule.output_modifier = ruleinfo.output.modifier
                rule.set_output(*ruleinfo.output.paths, **ruleinfo.output.kwpaths)
            if ruleinfo.params:
                rule.set_params(*ruleinfo.params[0], **ruleinfo.params[1])
            # handle default resources
            if self.default_resources is not None:
                rule.resources = copy.deepcopy(self.default_resources.parsed)
            if ruleinfo.threads is not None:
                if (
                    not isinstance(ruleinfo.threads, int)
                    and not isinstance(ruleinfo.threads, float)
                    and not callable(ruleinfo.threads)
                ):
                    raise RuleException(
                        "Threads value has to be an integer, float, or a callable.",
                        rule=rule,
                    )
                if name in self.overwrite_threads:
                    rule.resources["_cores"] = self.overwrite_threads[name]
                else:
                    if isinstance(ruleinfo.threads, float):
                        ruleinfo.threads = int(ruleinfo.threads)
                    rule.resources["_cores"] = ruleinfo.threads
            if ruleinfo.shadow_depth:
                if ruleinfo.shadow_depth not in (
                    True,
                    "shallow",
                    "full",
                    "minimal",
                    "copy-minimal",
                ):
                    raise RuleException(
                        "Shadow must either be 'minimal', 'copy-minimal', 'shallow', 'full', "
                        "or True (equivalent to 'full')",
                        rule=rule,
                    )
                if ruleinfo.shadow_depth is True:
                    rule.shadow_depth = "full"
                    logger.warning(
                        f"Shadow is set to True in rule {rule} (equivalent to 'full'). "
                        "It's encouraged to use the more explicit options "
                        "'minimal|copy-minimal|shallow|full' instead."
                    )
                else:
                    rule.shadow_depth = ruleinfo.shadow_depth
            if ruleinfo.resources:
                args, resources = ruleinfo.resources
                if args:
                    raise RuleException("Resources have to be named.")
                if not all(
                    map(
                        lambda r: isinstance(r, int)
                        or isinstance(r, str)
                        or callable(r),
                        resources.values(),
                    )
                ):
                    raise RuleException(
                        "Resources values have to be integers, strings, or callables (functions)",
                        rule=rule,
                    )
                rule.resources.update(resources)
            if name in self.overwrite_resources:
                rule.resources.update(self.overwrite_resources[name])

            if ruleinfo.priority:
                if not isinstance(ruleinfo.priority, int) and not isinstance(
                    ruleinfo.priority, float
                ):
                    raise RuleException(
                        "Priority values have to be numeric.", rule=rule
                    )
                rule.priority = ruleinfo.priority

            if ruleinfo.retries:
                if not isinstance(ruleinfo.retries, int) or ruleinfo.retries < 0:
                    raise RuleException(
                        "Retries values have to be integers >= 0", rule=rule
                    )
            rule.restart_times = (
                self.restart_times if ruleinfo.retries is None else ruleinfo.retries
            )

            if ruleinfo.version:
                rule.version = ruleinfo.version
            if ruleinfo.log:
                rule.log_modifier = ruleinfo.log.modifier
                rule.set_log(*ruleinfo.log.paths, **ruleinfo.log.kwpaths)
            if ruleinfo.message:
                rule.message = ruleinfo.message
            if ruleinfo.benchmark:
                rule.benchmark_modifier = ruleinfo.benchmark.modifier
                rule.benchmark = ruleinfo.benchmark.paths
            if not self.run_local:
                group = self.overwrite_groups.get(name) or ruleinfo.group
                if group is not None:
                    rule.group = group
            if ruleinfo.wrapper:
                rule.conda_env = snakemake.wrapper.get_conda_env(
                    ruleinfo.wrapper, prefix=self.wrapper_prefix
                )
                # TODO retrieve suitable singularity image

            if ruleinfo.env_modules:
                # If using environment modules and they are defined for the rule,
                # ignore conda and singularity directive below.
                # The reason is that this is likely intended in order to use
                # a software stack specifically compiled for a particular
                # HPC cluster.
                invalid_rule = not (
                    ruleinfo.script
                    or ruleinfo.wrapper
                    or ruleinfo.shellcmd
                    or ruleinfo.notebook
                )
                if invalid_rule:
                    raise RuleException(
                        "envmodules directive is only allowed with "
                        "shell, script, notebook, or wrapper directives (not with run or the template_engine)",
                        rule=rule,
                    )
                from snakemake.deployment.env_modules import EnvModules

                rule.env_modules = EnvModules(*ruleinfo.env_modules)

            if ruleinfo.conda_env:
                if not (
                    ruleinfo.script
                    or ruleinfo.wrapper
                    or ruleinfo.shellcmd
                    or ruleinfo.notebook
                ):
                    raise RuleException(
                        "Conda environments are only allowed "
                        "with shell, script, notebook, or wrapper directives "
                        "(not with run or template_engine).",
                        rule=rule,
                    )

                if isinstance(ruleinfo.conda_env, Path):
                    ruleinfo.conda_env = str(ruleinfo.conda_env)

                rule.conda_env = ruleinfo.conda_env

            invalid_rule = not (
                ruleinfo.script
                or ruleinfo.wrapper
                or ruleinfo.shellcmd
                or ruleinfo.notebook
            )
            if ruleinfo.container_img:
                if invalid_rule:
                    raise RuleException(
                        "Singularity directive is only allowed "
                        "with shell, script, notebook or wrapper directives "
                        "(not with run or template_engine).",
                        rule=rule,
                    )
                rule.container_img = ruleinfo.container_img
                rule.is_containerized = ruleinfo.is_containerized
            elif self.global_container_img:
                if not invalid_rule and ruleinfo.container_img != False:
                    # skip rules with run directive or empty image
                    rule.container_img = self.global_container_img
                    rule.is_containerized = self.global_is_containerized

            rule.norun = ruleinfo.norun
            if ruleinfo.name is not None:
                rule.name = ruleinfo.name
            rule.docstring = ruleinfo.docstring
            rule.run_func = ruleinfo.func
            rule.shellcmd = ruleinfo.shellcmd
            rule.script = ruleinfo.script
            rule.notebook = ruleinfo.notebook
            rule.wrapper = ruleinfo.wrapper
            rule.template_engine = ruleinfo.template_engine
            rule.cwl = ruleinfo.cwl
            rule.basedir = self.current_basedir

            if ruleinfo.handover:
                if not ruleinfo.resources:
                    # give all available resources to the rule
                    rule.resources.update(
                        {
                            name: val
                            for name, val in self.global_resources.items()
                            if val is not None
                        }
                    )
                # This becomes a local rule, which might spawn jobs to a cluster,
                # depending on its configuration (e.g. nextflow config).
                self._localrules.add(rule.name)
                rule.is_handover = True

            if ruleinfo.cache:
                if len(rule.output) > 1:
                    if not rule.output[0].is_multiext:
                        raise WorkflowError(
                            "Rule is marked for between workflow caching but has multiple output files. "
                            "This is only allowed if multiext() is used to declare them (see docs on between "
                            "workflow caching).",
                            rule=rule,
                        )
                if not self.enable_cache:
                    logger.warning(
                        "Workflow defines that rule {} is eligible for caching between workflows "
                        "(use the --cache argument to enable this).".format(rule.name)
                    )
                else:
                    if ruleinfo.cache is True or "omit-software" or "all":
                        self.cache_rules[rule.name] = (
                            "all" if ruleinfo.cache is True else ruleinfo.cache
                        )
                    else:
                        raise WorkflowError(
                            "Invalid value for cache directive. Use True or 'omit-software'.",
                            rule=rule,
                        )
            if ruleinfo.benchmark and self.get_cache_mode(rule):
                raise WorkflowError(
                    "Rules with a benchmark directive may not be marked as eligible "
                    "for between-workflow caching at the same time. The reason is that "
                    "when the result is taken from cache, there is no way to fill the benchmark file with "
                    "any reasonable values. Either remove the benchmark directive or disable "
                    "between-workflow caching for this rule.",
                    rule=rule,
                )

            if ruleinfo.default_target is True:
                self.default_target = rule.name
            elif not (ruleinfo.default_target is False):
                raise WorkflowError(
                    "Invalid argument for 'default_target:' directive. Only True allowed. "
                    "Do not use the directive for rules that shall not be the default target. ",
                    rule=rule,
                )

            if ruleinfo.localrule is True:
                self._localrules.add(rule.name)

            ruleinfo.func.__name__ = f"__{rule.name}"
            self.globals[ruleinfo.func.__name__] = ruleinfo.func

            rule_proxy = RuleProxy(rule)
            if orig_name is not None:
                setattr(self.globals["rules"], orig_name, rule_proxy)
            setattr(self.globals["rules"], rule.name, rule_proxy)

            if checkpoint:
                self.globals["checkpoints"].register(rule, fallback_name=orig_name)
            rule.ruleinfo = ruleinfo
            return ruleinfo.func

        return decorate

    def docstring(self, string):
        def decorate(ruleinfo):
            ruleinfo.docstring = string.strip()
            return ruleinfo

        return decorate

    def input(self, *paths, **kwpaths):
        def decorate(ruleinfo):
            ruleinfo.input = InOutput(paths, kwpaths, self.modifier.path_modifier)
            return ruleinfo

        return decorate

    def output(self, *paths, **kwpaths):
        def decorate(ruleinfo):
            ruleinfo.output = InOutput(paths, kwpaths, self.modifier.path_modifier)
            return ruleinfo

        return decorate

    def params(self, *params, **kwparams):
        def decorate(ruleinfo):
            ruleinfo.params = (params, kwparams)
            return ruleinfo

        return decorate

    def register_wildcard_constraints(
        self, *wildcard_constraints, **kwwildcard_constraints
    ):
        def decorate(ruleinfo):
            ruleinfo.wildcard_constraints = (
                wildcard_constraints,
                kwwildcard_constraints,
            )
            return ruleinfo

        return decorate

    def cache_rule(self, cache):
        def decorate(ruleinfo):
            ruleinfo.cache = cache
            return ruleinfo

        return decorate

    def default_target_rule(self, value):
        def decorate(ruleinfo):
            ruleinfo.default_target = value
            return ruleinfo

        return decorate

    def localrule(self, value):
        def decorate(ruleinfo):
            ruleinfo.localrule = value
            return ruleinfo

        return decorate

    def message(self, message):
        def decorate(ruleinfo):
            ruleinfo.message = message
            return ruleinfo

        return decorate

    def benchmark(self, benchmark):
        def decorate(ruleinfo):
            ruleinfo.benchmark = InOutput(benchmark, {}, self.modifier.path_modifier)
            return ruleinfo

        return decorate

    def conda(self, conda_env):
        def decorate(ruleinfo):
            ruleinfo.conda_env = conda_env
            return ruleinfo

        return decorate

    def container(self, container_img):
        def decorate(ruleinfo):
            # Explicitly set container_img to False if None is passed, indicating that
            # no container image shall be used, also not a global one.
            ruleinfo.container_img = (
                container_img if container_img is not None else False
            )
            ruleinfo.is_containerized = False
            return ruleinfo

        return decorate

    def containerized(self, container_img):
        def decorate(ruleinfo):
            ruleinfo.container_img = container_img
            ruleinfo.is_containerized = True
            return ruleinfo

        return decorate

    def envmodules(self, *env_modules):
        def decorate(ruleinfo):
            ruleinfo.env_modules = env_modules
            return ruleinfo

        return decorate

    def global_container(self, container_img):
        self.global_container_img = container_img
        self.global_is_containerized = False

    def global_containerized(self, container_img):
        self.global_container_img = container_img
        self.global_is_containerized = True

    def threads(self, threads):
        def decorate(ruleinfo):
            ruleinfo.threads = threads
            return ruleinfo

        return decorate

    def retries(self, retries):
        def decorate(ruleinfo):
            ruleinfo.retries = retries
            return ruleinfo

        return decorate

    def shadow(self, shadow_depth):
        def decorate(ruleinfo):
            ruleinfo.shadow_depth = shadow_depth
            return ruleinfo

        return decorate

    def resources(self, *args, **resources):
        def decorate(ruleinfo):
            ruleinfo.resources = (args, resources)
            return ruleinfo

        return decorate

    def priority(self, priority):
        def decorate(ruleinfo):
            ruleinfo.priority = priority
            return ruleinfo

        return decorate

    def version(self, version):
        def decorate(ruleinfo):
            ruleinfo.version = version
            return ruleinfo

        return decorate

    def group(self, group):
        def decorate(ruleinfo):
            ruleinfo.group = group
            return ruleinfo

        return decorate

    def log(self, *logs, **kwlogs):
        def decorate(ruleinfo):
            ruleinfo.log = InOutput(logs, kwlogs, self.modifier.path_modifier)
            return ruleinfo

        return decorate

    def handover(self, value):
        def decorate(ruleinfo):
            ruleinfo.handover = value
            return ruleinfo

        return decorate

    def shellcmd(self, cmd):
        def decorate(ruleinfo):
            ruleinfo.shellcmd = cmd
            return ruleinfo

        return decorate

    def script(self, script):
        def decorate(ruleinfo):
            ruleinfo.script = script
            return ruleinfo

        return decorate

    def notebook(self, notebook):
        def decorate(ruleinfo):
            ruleinfo.notebook = notebook
            return ruleinfo

        return decorate

    def wrapper(self, wrapper):
        def decorate(ruleinfo):
            ruleinfo.wrapper = wrapper
            return ruleinfo

        return decorate

    def template_engine(self, template_engine):
        def decorate(ruleinfo):
            ruleinfo.template_engine = template_engine
            return ruleinfo

        return decorate

    def cwl(self, cwl):
        def decorate(ruleinfo):
            ruleinfo.cwl = cwl
            return ruleinfo

        return decorate

    def norun(self):
        def decorate(ruleinfo):
            ruleinfo.norun = True
            return ruleinfo

        return decorate

    def name(self, name):
        def decorate(ruleinfo):
            ruleinfo.name = name
            return ruleinfo

        return decorate

    def run(self, func):
        return RuleInfo(func)

    def module(
        self,
        name,
        snakefile=None,
        meta_wrapper=None,
        config=None,
        skip_validation=False,
        replace_prefix=None,
        prefix=None,
    ):
        self.modules[name] = ModuleInfo(
            self,
            name,
            snakefile=snakefile,
            meta_wrapper=meta_wrapper,
            config=config,
            skip_validation=skip_validation,
            replace_prefix=replace_prefix,
            prefix=prefix,
        )

    def userule(
        self,
        rules=None,
        from_module=None,
        exclude_rules=None,
        name_modifier=None,
        lineno=None,
    ):
        def decorate(maybe_ruleinfo):
            if from_module is not None:
                try:
                    module = self.modules[from_module]
                except KeyError:
                    raise WorkflowError(
                        "Module {} has not been registered with 'module' statement before using it in 'use rule' statement.".format(
                            from_module
                        )
                    )
                module.use_rules(
                    rules,
                    name_modifier,
                    exclude_rules=exclude_rules,
                    ruleinfo=None if callable(maybe_ruleinfo) else maybe_ruleinfo,
                    skip_global_report_caption=self.report_text
                    is not None,  # do not overwrite existing report text via module
                )
            else:
                # local inheritance
                if self.modifier.skip_rule(name_modifier):
                    # The parent use rule statement is specific for a different particular rule
                    # hence this local use rule statement can be skipped.
                    return

                if len(rules) > 1:
                    raise WorkflowError(
                        "'use rule' statement from rule in the same module must declare a single rule but multiple rules are declared."
                    )
                orig_rule = self._rules[self.modifier.modify_rulename(rules[0])]
                ruleinfo = maybe_ruleinfo if not callable(maybe_ruleinfo) else None
                with WorkflowModifier(
                    self,
                    parent_modifier=self.modifier,
                    rulename_modifier=get_name_modifier_func(
                        rules, name_modifier, parent_modifier=self.modifier
                    ),
                    ruleinfo_overwrite=ruleinfo,
                ):
                    # A copy is necessary to avoid leaking modifications in case of multiple inheritance statements.
                    import copy

                    orig_ruleinfo = copy.copy(orig_rule.ruleinfo)
                    self.rule(
                        name=name_modifier,
                        lineno=lineno,
                        snakefile=self.included_stack[-1],
                    )(orig_ruleinfo)

        return decorate

    @staticmethod
    def _empty_decorator(f):
        return f


class Subworkflow:
    def __init__(self, workflow, name, snakefile, workdir, configfile):
        self.workflow = workflow
        self.name = name
        self._snakefile = snakefile
        self._workdir = workdir
        self.configfile = configfile

    @property
    def snakefile(self):
        if self._snakefile is None:
            return os.path.abspath(os.path.join(self.workdir, "Snakefile"))
        if not os.path.isabs(self._snakefile):
            return os.path.abspath(os.path.join(self.workflow.basedir, self._snakefile))
        return self._snakefile

    @property
    def workdir(self):
        workdir = "." if self._workdir is None else self._workdir
        if not os.path.isabs(workdir):
            return os.path.abspath(os.path.join(self.workflow.basedir, workdir))
        return workdir

    def target(self, paths):
        if not_iterable(paths):
            path = paths
            path = (
                path
                if os.path.isabs(path) or path.startswith("root://")
                else os.path.join(self.workdir, path)
            )
            return flag(path, "subworkflow", self)
        return [self.target(path) for path in paths]

    def targets(self, dag):
        def relpath(f):
            if f.startswith(self.workdir):
                return os.path.relpath(f, start=self.workdir)
            # do not adjust absolute targets outside of workdir
            return f

        return [
            relpath(f)
            for job in dag.jobs
            for f in job.subworkflow_input
            if job.subworkflow_input[f] is self
        ]


def srcdir(path):
    """Return the absolute path, relative to the source directory of the current Snakefile."""
    if not workflow.included_stack:
        return None
    return workflow.current_basedir.join(path).get_path_or_uri()
