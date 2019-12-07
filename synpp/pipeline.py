import importlib
import inspect
import hashlib, json
import networkx as nx
import pickle
import datetime
import shutil
import logging

from .general import PipelineError
from .parallel import ParallelMasterContext
from .progress import ProgressContext

class NoDefaultValue:
    pass

class StageInstance:
    def __init__(self, instance, name):
        self.instance = instance
        self.name = name

    def parameterize(self, parameters):
        return ParameterizedStage(self.instance, self.name)

    def configure(self, context):
        if hasattr(self.instance, "configure"):
            return self.instance.configure(context)

    def validate(self, context):
        if hasattr(self.instance, "validate"):
            return self.instance.validate(context)

        return None

    def execute(self, context):
        if hasattr(self.instance, "execute"):
            return self.instance.execute(context)
        else:
            raise RuntimeError("Stage %s does not have execute method" % self.name)

def resolve_stage(descriptor):
    if isinstance(descriptor, str):
        try:
            # Try to get the module referenced by the string
            descriptor = importlib.import_module(descriptor)
        except ModuleNotFoundError:
            # Not a module, but maybe a class?
            parts = descriptor.split(".")

            module = importlib.import_module(".".join(parts[:-1]))
            constructor = getattr(module, parts[-1])
            descriptor = constructor()

    if inspect.ismodule(descriptor):
        return StageInstance(descriptor, descriptor.__name__)

    if inspect.isclass(descriptor):
        return StageInstance(descriptor(), "%s.%s" % (descriptor.__module__, descriptor.__name__))

    clazz = descriptor.__class__
    return StageInstance(descriptor, "%s.%s" % (clazz.__module__, clazz.__name__))

def parameterize_stage(instance, context, parameters):
    parameter_values = {}

    for name, default_value in context.required_parameters.items():
        if name in parameters:
            parameter_values[name] = parameters[name]
        elif not type(default_value) == NoDefaultValue:
            parameter_values[name] = default_value
        else:
            raise PipelineError("Parameter '%s' missing for stage '%s'" % (name, instance.name))

    return ParameterizedStage(instance, parameters, context)

def parameterize_name(name, parameters):
    values = ["%s=%s" % (name, value) for name, value in parameters.items()]
    return "%s(%s)" % (name, ",".join(values))

def hash_name(name, parameters):
    if len(parameters) > 0:
        hash = hashlib.md5()
        hash.update(json.dumps(parameters, sort_keys = True).encode("ascii"))
        return "%s__%s" % (name, hash.hexdigest())
    else:
        return name

class ParameterizedStage:
    def __init__(self, instance, parameters, configuration_context):
        self.instance = instance
        self.parameters = parameters
        self.configuration_context = configuration_context

        self.parameterized_name = parameterize_name(instance.name, parameters)
        self.hashed_name = hash_name(instance.name, parameters)

    def configure(self, context):
        return self.instance.configure(context)

    def execute(self, context):
        return self.instance.execute(context)

    def validate(self, context):
        return self.instance.validate(context)

class ConfigurationContext:
    def __init__(self, base_config, base_parameters):
        self.base_config = base_config
        self.base_parameters = base_parameters

        self.required_config = {}
        self.required_parameters = {}

        self.required_stages = []
        self.aliases = {}

    def config(self, option, default = NoDefaultValue()):
        if option in self.base_config:
            self.required_config[option] = self.base_config[option]
        elif not isinstance(default, NoDefaultValue):
            if option in self.required_config and not self.required_config[option] == default:
                raise PipelineError("Got multiple default values for config option: %s" % option)

            self.required_config[option] = default

        if not option in self.required_config:
            raise PipelineError("Config option is not available: %s" % option)

        return self.required_config[option]

    def parameter(self, name, default = NoDefaultValue()):
        if name in self.base_parameters:
            self.required_parameters[name] = self.base_parameters[name]
        elif not isinstance(default, NoDefaultValue):
            if name in self.required_parameters and not self.required_parameters[name] == default:
                raise PipelineError("Got multiple default values for parameter: %s" % name)

            self.required_parameters[name] = default

        if not name in self.required_parameters:
            raise PipelineError("Config option is not available: %s" % name)

        return self.required_parameters[name]

    def stage(self, descriptor, parameters = {}, alias = None):
        definition = {
            "descriptor": descriptor, "parameters": parameters
        }

        if not definition in self.required_stages:
            self.required_stages.append(definition)

            if not alias is None:
                self.aliases[alias] = definition

class ValidateContext:
    def __init__(self, configuration_context):
        self.configuration_context = configuration_context

    def parameter(self, name):
        if not name in self.configuration_context.required_parameters:
            raise PipelineError("Parameter %s is not requested" % name)

        return self.configuration_context.required_parameters[name]

    def config(self, name):
        if not name in self.configuration_context.required_config:
            raise PipelineError("Config option %s is not requested" % name)

        return self.configuration_context.required_config[name]

class ExecuteContext:
    def __init__(self, configuration_context, dependencies, pipeline_config, logger):
        self.configuration_context = configuration_context
        self.dependencies = dependencies
        self.pipeline_config = pipeline_config
        self.logger = logger
        self.info_data = {}

    def parameter(self, name):
        if not name in self.configuration_context.required_parameters:
            raise PipelineError("Parameter %s is not requested" % name)

        return self.configuration_context.required_parameters[name]

    def config(self, name):
        if not name in self.configuration_context.required_config:
            raise PipelineError("Config option %s is not requested" % name)

        return self.configuration_context.required_config[name]

    def stage(self, name, parameters = {}):
        definition = {
            "descriptor": name, "parameters": parameters
        }

        if name in self.configuration_context.aliases:
            if len(parameters) > 0:
                raise PipelineError("Cannot define parameters for aliased stage")

            definition = self.configuration_context.aliases[name]

        if not definition in self.configuration_context.required_stages:
            raise PipelineError("Stage '%s' with parameters %s is not requested" % (definition["descriptor"], definition["parameters"]))

        return self.dependencies[self.configuration_context.required_stages.index(definition)]

    def info(self, name, value):
        self.info_data[name] = value

    def parallel(self, data = {}, processes = None):
        config = self.configuration_context.required_config
        parameters = self.configuration_context.required_parameters

        if processes is None and "processes" in self.pipeline_config:
            processes = self.pipeline_config["processes"]

        return ParallelMasterContext(data, config, parameters, processes)

    def progress(self, iterable = None, label = None, total = None, minimum_interval = 1.0):
        if minimum_interval is None and "progress_interval" in self.pipeline_config:
            minimum_interval = self.pipeline_config["progress_interval"]

        return ProgressContext(iterable, total, label, self.logger, minimum_interval)

def run(definitions, config = {}, working_directory = None, verbose = False, logger = logging.getLogger("synpp")):
    # 0) Construct pipeline config
    pipeline_config = {}
    if "processes" in config: pipeline_config["processes"] = config["processes"]
    if "progress_interval" in config: pipeline_config["progress_interval"] = config["progress_interval"]

    # 1) Construct stage objects
    pending_definitions = definitions[:]

    unparameterized_registry = {}
    hashed_registry = {}
    required_names = []

    dependencies = {}
    contexts = {}

    while len(pending_definitions) > 0:
        definition = pending_definitions.pop(0)
        parameters = definition["parameters"] if "parameters" in definition else {}

        # Resolve the underlying code of the stage
        unparameterized_stage = resolve_stage(definition["descriptor"])
        unparameterized_name = unparameterized_stage.name

        if not unparameterized_name in unparameterized_registry:
            unparameterized_registry[unparameterized_name] = unparameterized_stage
        else:
            unparameterized_stage = unparameterized_registry[unparameterized_name]

        # Call the configure method of the stage and obtain parameters
        context = ConfigurationContext(config, parameters)
        unparameterized_stage.configure(context)

        # Create a parameterized version of the stage
        parameterized_stage = parameterize_stage(unparameterized_stage, context, parameters)
        hashed_name = parameterized_stage.hashed_name

        if hashed_name in hashed_registry:
            parameterized_stage = hashed_registry[hashed_name]
        else:
            hashed_registry[hashed_name] = parameterized_stage

            # Go through dependencies
            for index, dependency in enumerate(context.required_stages):
                pending_definitions.append({
                    "descriptor": dependency["descriptor"],
                    "parameters": dependency["parameters"],
                    ":child": { "name": hashed_name, "index": index, "size": len(context.required_stages) }
                })

        # Add to dependnecy tree
        if ":child" in definition:
            child = definition[":child"]

            if not child["name"] in dependencies:
                dependencies[child["name"]] = [None] * child["size"]

            dependencies[child["name"]][child["index"]] = hashed_name

        # Make sure we capture the results
        if definition in definitions:
            required_names.append(hashed_name)

    logger.info("Found %d stages" % len(hashed_registry))

    # 2) Order stages
    graph = nx.DiGraph()

    for name in hashed_registry.keys():
        graph.add_node(name)

    for child_name, parent_names in dependencies.items():
        for parent_name in parent_names:
            graph.add_edge(parent_name, child_name)

    for cycle in nx.cycles.simple_cycles(graph):
        cycle = [hashed_registry[item].parameterized_name for item in cycle]
        raise PipelineError("Found cycle: %s" % " -> ".join(cycle))

    sorted_names = list(nx.topological_sort(graph))

    # 3) Load information about stages
    meta = {}

    if not working_directory is None:
        try:
            with open("%s/pipeline.json" % working_directory) as f:
                meta = json.load(f)
                logger.info("Found pipeline metadata in %s/pipeline.json" % working_directory)
        except FileNotFoundError:
            logger.info("Did not find pipeline metadata in %s/pipeline.json" % working_directory)

    # 4) Devalidate stages

    # 4.1) Devalidate if they are required
    stale_names = set(required_names)

    logger.info("Devalidating %d requested stages:" % len(stale_names))
    for name in required_names: logger.info("- %s" % name)

    # 4.2) Devalidate if not in meta
    partial_stages = set(sorted_names) - meta.keys()
    stale_names |= partial_stages

    logger.info("Devalidating %d stages without meta data:" % len(partial_stages))
    for name in partial_stages: logger.info("- %s" % name)

    # 4.3) Devalidate if configuration values have changed
    partial_config, partial_stages = {}, set()

    for name in sorted_names:
        if not name in stale_names and name in meta:
            for key, value in meta[name]["config"].items():
                if not key in config or not config[key] == value:
                    stale_names.add(name)

                    partial_config[key] = (value, config[key])
                    partial_stages.add(name)

    logger.info("Devalidating %d stages because config has changed ..." % len(partial_stages))
    for name in partial_stages: logger.info("- %s" % name)
    logger.info("... with the following values:")
    for key, (v1, v2) in partial_config.items(): logger.info("- %s: %s -> %s" % (key, v1, v2))

    # 4.4) Devalidate if parent has been updated
    partial_stages = {}

    for name in sorted_names:
        if not name in stale_names and name in meta:
            for parent_name, parent_update in meta[name]["parents"].items():
                if not parent_name in meta:
                    stale_names.add(name)
                    partial_stages[name] = parent_name
                else:
                    if meta[parent_name]["updated"] > parent_update:
                        stale_names.add(name)
                        partial_stages[name] = parent_name

    logger.info("Devalidating %d stages because parent was updated:" % len(partial_stages))
    for p in partial_stages.items(): logger.info("- %s (because of %s)" % p)

    # 4.5) Devalidate if parents are not the same anymore
    for name in sorted_names:
        if not name in stale_names and name in meta:
            cached_names = meta[name]["parents"].keys()

            if not name in dependencies:
                if len(cached_names) > 0:
                    stale_names.add(name)
                    partial_stages.add(name)
            elif not cached_names == set(dependencies[name]):
                stale_names.add(name)
                partial_stages.add(name)

    logger.info("Devalidating %d stages because parents have changed:" % len(partial_stages))
    for name in partial_stages: logger.info("- %s" % name)

    # 4.6) Manually devalidate stages
    partial_stages = set()

    for name in sorted_names:
        stage = hashed_registry[name]
        context = ValidateContext(stage.configuration_context)

        validation_token = stage.validate(context)
        existing_token = meta[name]["validation_token"] if name in meta and "validation_token" in meta[name] else None

        if not validation_token == existing_token:
            stale_names.add(name)
            partial_stages.add(name)

    logger.info("Devalidating %d stages by user-defined validation" % len(partial_stages))
    for name in partial_stages: logger.info("- %s" % name)

    # 4.7) Devalidate descendants of devalidated stages
    partial_stages = set()

    for name in set(stale_names):
        for descendant in nx.descendants(graph, name):
            if not descendant in stale_names:
                stale_names.add(descendant)
                partial_stages.add(descendant)

    logger.info("Devalidating %d stages because with stale ancestors:" % len(partial_stages))
    for name in partial_stages: logger.info("- %s" % name)

    # 5) Reset meta information
    for name in stale_names:
        if name in meta:
            del meta[name]

    if not working_directory is None:
        with open("%s/pipeline.json" % working_directory, "w+") as f:
            json.dump(meta, f)

    logger.info("Successfully reset meta data")

    # 6) Execute stages
    results = [None] * len(definitions)
    cache = {}

    for name in sorted_names:
        if name in stale_names:
            logger.info("Executing stage %s ..." % name)
            stage = hashed_registry[name]

            # Load the dependencies, either from cache or from file
            stage_dependencies = []
            if name in dependencies:
                if working_directory is None:
                    stage_dependencies = [cache[parent] for parent in dependencies[name]]
                else:
                    for child in dependencies[name]:
                        with open("%s/%s.p" % (working_directory, child), "rb") as f:
                            logger.info("Loading cache for %s ..." % child)
                            stage_dependencies.append(pickle.load(f))

            context = ExecuteContext(stage.configuration_context, stage_dependencies, pipeline_config, logger)
            result = stage.execute(context)
            validation_token = stage.validate(ValidateContext(stage.configuration_context))

            if name in required_names:
                results[required_names.index(name)] = result

            if working_directory is None:
                cache[name] = result
            else:
                with open("%s/%s.p" % (working_directory, name), "wb+") as f:
                    logger.info("Writing cache for %s" % name)
                    pickle.dump(result, f)

            # Update meta information
            meta[name] = {
                "config": stage.configuration_context.required_config,
                "updated": datetime.datetime.utcnow().timestamp(),
                "parents": {
                    parent: meta[parent]["updated"] for parent in dependencies[name]
                } if name in dependencies else {},
                "info": context.info_data,
                "validation_token": validation_token
            }

            if not working_directory is None:
                with open("%s/pipeline.json" % working_directory, "w+") as f:
                    json.dump(meta, f)

            logger.info("Finished running %s." % name)

    if verbose:
        info = {}

        for name in sorted(meta.keys()):
            info.update(meta[name]["info"])

        return {
            "results": results,
            "stale": stale_names,
            "info": info
        }
    else:
        return results




















#
