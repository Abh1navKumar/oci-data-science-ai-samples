import os
import re
import ads
import oci
import requests

from flask import Flask, render_template, jsonify, abort, request
from ads.common.oci_resource import OCIResource
from ads.jobs import Job
from ads.jobs.builders.infrastructure.dsc_job import DataScienceJobRun


app = Flask(__name__, template_folder=os.path.dirname(__file__))


def instance_principal_available():
    try:
        requests.get(
            oci.auth.signers.InstancePrincipalsSecurityTokenSigner.GET_REGION_URL,
            headers=oci.auth.signers.InstancePrincipalsDelegationTokenSigner.METADATA_AUTH_HEADERS,
            timeout=1
        )
        return True
    except:
        return False


def get_authentication():
    if os.path.exists(os.path.expanduser(oci.config.DEFAULT_LOCATION)):
        auth = dict(config=oci.config.from_file())
    elif oci.auth.signers.resource_principals_signer.OCI_RESOURCE_PRINCIPAL_VERSION in os.environ:
        config = {}
        signer = oci.auth.signers.get_resource_principals_signer()
        auth = dict(config=config, signer=signer)
    elif instance_principal_available():
        config = {}
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        auth = dict(config=config, signer=signer)
    else:
        raise Exception("Cannot determine authentication method.")
    return auth


if not os.path.exists(os.path.expanduser(oci.config.DEFAULT_LOCATION)):
    ads.set_auth('resource_principal')


def check_ocid(ocid):
    if not re.match(r'ocid[0-9].[a-z]+.oc[0-9].[a-z]{3}.[a-z0-9]+', ocid):
        abort(404, f"Invalid OCID: {ocid}")

def check_project_id(project_id):
    if not re.match(r'ocid[0-9].datascienceproject.oc[0-9].[a-z]{3}.[a-z0-9]+', project_id):
        abort(404, f"Invalid Project OCID: {project_id}")


def check_compartment_id(compartment_id):
    if not re.match(r'ocid[0-9].compartment.oc[0-9]..[a-z0-9]+', compartment_id):
        abort(404, f"Invalid Compartment OCID: {compartment_id}")


def check_compartment_project(compartment_id, project_id):
    if str(project_id).lower() == "all":
        project_id = None
    else:
        check_project_id(project_id)
        # Lookup compartment when project ID is valid but no compartment is given.
        if not compartment_id:
            compartment_id = OCIResource.get_compartment_id(project_id)
    check_compartment_id(compartment_id)
    return compartment_id, project_id


@app.route("/")
@app.route("/<project_id>")
@app.route("/<compartment_id>/<project_id>")
def job_monitor(compartment_id=None, project_id=None):
    if project_id == "favicon.ico":
        abort(404)

    limit = request.args.get("limit", 10)

    if project_id:
        if project_id == "all":
            project_id = None
        elif not re.match(r'ocid[0-9].datascienceproject.oc[0-9].[a-z]{3}.[a-z0-9]+', project_id):
            return jsonify(error="Invalid Project ID"), 400
        elif not compartment_id:
            compartment_id = OCIResource.get_compartment_id(project_id)

        if not re.match(r'ocid[0-9].compartment.oc[0-9]..[a-z0-9]+', compartment_id):
            return jsonify(error="Invalid Compartment ID"), 400
    else:
        compartment_id = None

    auth = get_authentication()
    if auth["config"]:
        tenancy_id = auth["config"]["tenancy"]
    else:
        tenancy_id = auth["signer"].tenancy_id

    compartments = oci.identity.IdentityClient(**auth).list_compartments(compartment_id=tenancy_id).data
    context = dict(
        compartment_id=compartment_id,
        project_id=project_id,
        compartments=compartments,
        limit=limit
    )
    return render_template(
        'job_monitor.html',
        **context
    )

@app.route("/jobs/<compartment_id>/<project_id>")
def list_jobs(compartment_id, project_id):
    compartment_id, project_id = check_compartment_project(compartment_id, project_id)
    limit = request.args.get("limit", 10)
    if not limit.isdigit():
        return jsonify({
            "error": "limit must be an integer.",
        })

    # Calling OCI API here instead of ADS API is faster :)
    jobs = oci.data_science.DataScienceClient(
        **get_authentication()
    ).list_jobs(
        compartment_id=compartment_id,
        project_id=project_id,
        lifecycle_state="ACTIVE",
        sort_by="timeCreated",
        sort_order="DESC",
        limit=int(limit) + 5
    ).data[:int(limit)]

    job_list = []
    for job in jobs:
        job_data = dict(name=job.display_name, id=job.id)
        job_data.update(
            ocid=job.id,
            html=render_template("job_accordion.html", job=job)
        )
        job_list.append(job_data)
    return jsonify({
        "limit": limit,
        "jobs": job_list
    })

@app.route("/job_runs/<job_id>")
def list_job_runs(job_id):
    job = Job.from_datascience_job(job_id)
    runs = job.run_list()
    run_list = []
    for run in runs:
        run_data = {
            "ocid": run.id,
            "html": render_template("job_run_template.html", run=run, job=job)
        }
        run_list.append(run_data)
    return jsonify({
        "job": job.to_dict(),
        "runs": run_list
    })

@app.route("/projects/<compartment_id>")
def list_projects(compartment_id):
    projects = oci.data_science.DataScienceClient(
        **get_authentication()
    ).list_projects(compartment_id=compartment_id).data
    projects = sorted(projects, key=lambda x: x.display_name)
    context = {
        "compartment_id": compartment_id,
        "projects": [
            {"display_name": project.display_name, "ocid": project.id} for project in projects
        ]
    }
    return jsonify(context)


def format_logs(logs):
    logs = sorted(logs, key=lambda x: x["time"] if x["time"] else "")
    for log in logs:
        if str(log["time"]).endswith("Z"):
            log["time"] = log["time"].split(".")[0].replace("T", " ")
        else:
            log["time"] = str(log["time"])
    logs = [log["time"] + " " + log["message"] for log in logs]
    print(f"{len(logs)} log messages.")
    return logs


@app.route("/logs/<job_run_ocid>")
def get_logs(job_run_ocid):
    print(f"Getting logs for {job_run_ocid}...")
    run = DataScienceJobRun.from_ocid(job_run_ocid)
    print(f"Status: {run.lifecycle_state} - {run.lifecycle_details}")
    if not run.log_id:
        logs = []
    else:
        logs = run.logs(limit=300)
        logs = format_logs(logs)
    context = {
        "ocid": job_run_ocid,
        "logs": logs,
        "status": run.lifecycle_state,
        "statusDetails": run.lifecycle_details,
        "stopped": True if run.lifecycle_state in DataScienceJobRun.TERMINAL_STATES else False
    }
    return jsonify(context)


@app.route("/delete/<job_ocid>")
def delete_job(job_ocid):
    job = Job.from_datascience_job(job_ocid)
    try:

        job.delete()
        error = None
    except oci.exceptions.ServiceError as ex:
        error = ex.message
    return jsonify({
        "ocid": job_ocid,
        "error": error
    })
