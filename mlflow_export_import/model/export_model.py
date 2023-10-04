"""
Exports a registered model, its versions and the version's run.
"""

import os
import click
from dataclasses import dataclass

import mlflow
from mlflow.exceptions import RestException

from mlflow_export_import.client.http_client import create_http_client, create_dbx_client
from mlflow_export_import.common.click_options import (
    opt_model,
    opt_output_dir,
    opt_notebook_formats,
    opt_stages,
    opt_versions,
    opt_export_latest_versions,
    opt_export_deleted_runs,
    opt_export_permissions,
    opt_export_version_model
)

from mlflow_export_import.common import utils, io_utils, model_utils
from mlflow_export_import.common.timestamp_utils import fmt_ts_millis
from mlflow_export_import.common import permissions_utils
from mlflow_export_import.common import MlflowExportImportException
from mlflow_export_import.run.export_run import export_run

_logger = utils.getLogger(__name__)

@dataclass()
class Options:
    stages: None
    versions: None
    export_latest_versions: bool
    export_deleted_runs: bool
    export_version_model: bool
    export_permissions: bool
    notebook_formats: []


def export_model(
        model_name,
        output_dir,
        stages = None,
        versions = None,
        export_latest_versions = False,
        export_version_model = False,
        export_permissions = False,
        export_deleted_runs = False,
        notebook_formats = None,
        mlflow_client = None
    ):
    """
    :param model_name: Registered model name.
    :param output_dir: Export directory.
    :param stages: Stages to export. Default is all stages. Values are Production, Staging, Archived and None.
    :param versions: List of versions to export.
    :param export_latest_versions: Export latest registered model versions instead of all versions.
    :param export_version_model: Export registered model version's 'cached' MLflow model.
    :param export_deleted_runs: Export deleted runs.
    :param export_permissions: Export Databricks permissions.
    :param notebook_formats: List of notebook formats to export. Values are SOURCE, HTML, JUPYTER or DBC.
    :param mlflow_client: MlflowClient
    :return: Returns bool and the model name (if export succeeded).
    """

    mlflow_client = mlflow_client or mlflow.MlflowClient()
    http_client = create_http_client(mlflow_client, model_name)
    dbx_client = create_dbx_client(mlflow_client)

    stages = _normalize_stages(stages)
    versions = versions if versions else []
    if len(stages) > 0 and len(versions) > 0:
        raise MlflowExportImportException(
            f"Both stages {stages} and versions {versions} cannot be set", http_status_code=400)

    opts = Options(stages, versions, export_latest_versions, export_deleted_runs, export_version_model, export_permissions, notebook_formats)

    try:
        _export_model(mlflow_client, http_client, dbx_client, model_name, output_dir, opts)
        return True, model_name
    except RestException as e:
        err_msg = { "model": model_name, "RestException": e.json  }
        if e.json.get("error_code") == "RESOURCE_DOES_NOT_EXIST":
            _logger.error({ **{"message": "Model does not exist"}, **err_msg})
        else:
            _logger.error({**{"message": "Model cannot be exported"}, **err_msg})
            import traceback
            traceback.print_exc()
        return False, model_name
    except Exception as e:
        _logger.error({ "model": model_name, "Exception": e })
        import traceback
        traceback.print_exc()
        return False, model_name


def _export_model(mlflow_client, http_client, dbx_client, model_name, output_dir, opts):
    ori_versions = model_utils.list_model_versions(mlflow_client, model_name, opts.export_latest_versions)
    msg = "latest" if opts.export_latest_versions else "all"
    _logger.info(f"Exporting model '{model_name}': found {len(ori_versions)} '{msg}' versions")

    if utils.importing_into_databricks() and opts.export_permissions:
        _model = http_client.get("databricks/registered-models/get", { "name": model_name })
        model = _model.pop("registered_model_databricks", None)
        permissions = permissions_utils.get_model_permissions(dbx_client, model["id"])
        _model["registered_model"] = model
    else:
        _model = http_client.get("registered-models/get", {"name": model_name})
        model = _model["registered_model"]
        permissions = None

    versions, failed_versions = _export_versions(mlflow_client, model, ori_versions, output_dir, opts)

    _adjust_model(model, versions)
    if permissions:
        model["permissions"] = permissions

    info_attr = {
        "num_target_stages": len(opts.stages),
        "num_target_versions": len(opts.versions),
        "num_src_versions": len(versions),
        "num_dst_versions": len(versions),
        "failed_versions": failed_versions,
        "export_latest_versions": opts.export_latest_versions,
        "export_permissions": opts.export_permissions
    }
    _model = { "registered_model": model }
    io_utils.write_export_file(output_dir, "model.json", __file__, _model, info_attr)
    _logger.info(f"Exported {len(versions)}/{len(ori_versions)} '{msg}' versions for model '{model_name}'")


def _export_versions(mlflow_client, model_dct, versions, output_dir, opts):
    aliases = model_dct.get("aliases", [])
    version_aliases = {}
    [ version_aliases.setdefault(x["version"], []).append(x["alias"]) for x in aliases ] # map of version => its aliases

    output_versions, failed_versions = ([], [])
    for j,vr in enumerate(versions):
        if len(opts.stages) > 0 and not vr.current_stage.lower() in opts.stages:
            continue
        if len(opts.versions) > 0 and not vr.version in opts.versions:
            continue
        _export_version(mlflow_client, vr, output_dir, version_aliases.get(vr.version,[]), output_versions, failed_versions, j, len(versions), opts)
    output_versions.sort(key=lambda x: x["version"], reverse=False)
    return output_versions, failed_versions


def _export_version(mlflow_client, vr, output_dir, aliases, output_versions, failed_versions, j, num_versions, opts):
    _output_dir = os.path.join(output_dir, vr.run_id)
    msg = { "name": vr.name, "version": vr.version, "stage": vr.current_stage, "aliases": aliases }
    _logger.info(f"Exporting model verson {j+1}/{num_versions}: {msg} to '{_output_dir}'")

    try:
        export_run(vr.run_id,
            _output_dir,
            export_deleted_runs = opts.export_deleted_runs,
            notebook_formats = opts.notebook_formats,
            mlflow_client = mlflow_client
        )
        vr_dct = model_utils.model_version_to_dict(vr)
        vr_dct["aliases"] = aliases

        run = mlflow_client.get_run(vr.run_id)
        vr_dct["_run_artifact_uri"] = run.info.artifact_uri
        experiment = mlflow_client.get_experiment(run.info.experiment_id)
        vr_dct["_experiment_name"] = experiment.name
        if opts.export_version_model:
            _output_dir = os.path.join(output_dir, "version_models", vr.version)
            vr_dct["_download_uri"] = model_utils.export_version_model(mlflow_client, vr, _output_dir)

        output_versions.append(vr_dct)

    except RestException as e:
        err_msg = { "model": vr.name, "version": vr.version, "run_id": vr.run_id, "RestException": e.json  }
        if e.json.get("error_code") == "RESOURCE_DOES_NOT_EXIST":
            err_msg = { **{"message": "Version run probably does not exist"}, **err_msg}
            _logger.error(err_msg)
        else:
            err_msg = { **{"message": "Version cannot be exported"}, **err_msg}
            _logger.error(err_msg)
            import traceback
            traceback.print_exc()
        failed_versions.append(err_msg)


def _adjust_model(model, versions):
    """
    1. Add human friendly timestamps to model and versions
    2. For aesthetic reasons reorder dict keys
    3. Rename ModelVersion 'latest_versions' (if it exists) to 'versions' key
    """

    def _reorder(model, key):
        val = model.pop(key, None)
        if val:
            model[key] = val

    def _adjust_timestamp(dct, key):
        dct[f"_{key}"] = fmt_ts_millis(dct.get(key))

    # add human friendly timestamps
    def _adjust_timestamps(dct):
        _adjust_timestamp(dct, "creation_timestamp")
        _adjust_timestamp(dct, "last_updated_timestamp")

    # add human friendly timestamps for model
    _adjust_timestamps(model)

    # reorder tags and aliases keys
    _reorder(model, "tags")
    _reorder(model, "aliases")

    # rename ModelVersion 'latest_versions' (if it exists) to 'versions'
    model["versions"] = versions
    model.pop("latest_versions", None)

    # add human friendly timestamps for versions
    for vr in versions:
        _adjust_timestamps(vr)


def _normalize_stages(stages):
    """
    Normalize polymorphic 'stages' variable. Fun stuff. ;)
    :param stages: Can be a string stage, string comma-delimited list of stages or None.
    :return: Returns list of stages as strings.
    """
    from mlflow.entities.model_registry import model_version_stages
    if stages is None:
        return []
    if isinstance(stages, str):
        if stages == "":
            return []
        stages = stages.split(",")
    stages = [stage.lower() for stage in stages]
    for stage in stages:
        if stage not in model_version_stages._CANONICAL_MAPPING:
            _logger.warning(f"stage '{stage}' must be one of: {model_version_stages.ALL_STAGES}")
    return stages


@click.command()
@opt_model
@opt_output_dir
@opt_stages
@opt_versions
@opt_export_latest_versions
@opt_export_version_model
@opt_export_deleted_runs
@opt_export_permissions
@opt_notebook_formats

def main(model, output_dir,
        stages, versions, export_latest_versions,
        export_deleted_runs,
        export_version_model,
        export_permissions,
        notebook_formats
    ):
    _logger.info("Options:")
    for k,v in locals().items():
        _logger.info(f"  {k}: {v}")
    versions = versions.split(",") if versions else []
    export_model(
        model_name = model,
        output_dir = output_dir,
        stages = stages,
        versions = versions,
        export_latest_versions = export_latest_versions,
        export_deleted_runs = export_deleted_runs,
        export_version_model = export_version_model,
        export_permissions = export_permissions,
        notebook_formats = notebook_formats
    )


if __name__ == "__main__":
    main()
