""" Unit normal normalization """

import json

import boto3
from cirrus.lambda_thread import LambdaThread
from cirrus.utils import get_all_keys, launch_lambdas, Timer

MAX_LAMBDAS = 400


class LocalRange(LambdaThread):
    """ Get the mean and standard deviation for this chunk """
    def __init__(self, s3_key, s3_bucket_input):
        LambdaThread.__init__(self)
        self.lamdba_dict = {
            "s3_bucket_input": s3_bucket_input,
            "s3_key": s3_key,
            "action": "LOCAL_RANGE",
            "normalization": "NORMAL"
        }


class LocalScale(LambdaThread):
    """ Subtract the global mean and divide by the standard
    deviation """
    def __init__(self, s3_key, s3_bucket_input, s3_bucket_output):
        LambdaThread.__init__(self)
        self.lamdba_dict = {
            "s3_bucket_input": s3_bucket_input,
            "s3_key": s3_key,
            "s3_bucket_output": s3_bucket_output,
            "action": "LOCAL_SCALE",
            "normalization": "NORMAL"
        }


def normal_scaler(s3_bucket_input, s3_bucket_output, objects=(), dry_run=False):
    """ Scale the values in a dataset to fit a unit normal distribution. """
    if not objects:
        # Allow user to specify objects, or otherwise get all objects.
        objects = get_all_keys(s3_bucket_input)

    # Calculate bounds for each chunk.
    timer = Timer("NORMAL_SCALING").set_step("LocalRange")
    launch_lambdas(LocalRange, objects, MAX_LAMBDAS, s3_bucket_input)

    timer.timestamp().set_step("Creating the global map")

    client = boto3.client("s3")
    f_ranges = get_global_map(s3_bucket_input, objects, client)

    timer.timestamp().set_step("Putting local maps")

    s3_resource = boto3.resource("s3")
    update_local_maps(s3_bucket_input, objects, f_ranges, client,
                      s3_resource)

    timer.timestamp().set_step("Local scaling")
    if not dry_run:
        # Scale the chunks and put them in the output bucket.
        launch_lambdas(LocalScale, objects, MAX_LAMBDAS,
                       s3_bucket_input, s3_bucket_output)

    timer.timestamp().set_step("Deleting local maps")

    # Delete any intermediary keys.
    for i in objects:
        s3_resource.Object(s3_bucket_input, str(i) + "_final_bounds").delete()

    timer.timestamp()


def get_global_map(s3_bucket_input, objects, client):
    """ Aggregate the sample means, std. devs., etc. to get the global map. """
    f_ranges = {}
    for i in objects:
        obj = client.get_object(Bucket=s3_bucket_input,
                                Key=str(i) + "_bounds")["Body"].read()
        local_map = json.loads(obj.decode("utf-8"))
        for idx in local_map:
            sample_mean_x_squared, sample_mean_x, n_samples = local_map[idx]
            if idx not in f_ranges:
                f_ranges[idx] = [
                    sample_mean_x_squared * n_samples,
                    sample_mean_x * n_samples,
                    n_samples]
            else:
                f_ranges[idx][0] += sample_mean_x_squared * n_samples
                f_ranges[idx][1] += sample_mean_x * n_samples
                f_ranges[idx][2] += n_samples
    return f_ranges


def update_local_maps(s3_bucket_input, objects, f_ranges,
                      s3_client, s3_resource):
    """ Update the local maps of means, std. devs., etc. """
    for i in objects:
        s3_obj = s3_resource.Object(s3_bucket_input, str(i) + "_bounds")
        obj = s3_obj.get()["Body"].read()
        local_map = json.loads(obj.decode("utf-8"))
        for idx in local_map:
            mean_x_sq = f_ranges[idx][0] / f_ranges[idx][2]
            mean = f_ranges[idx][1] / f_ranges[idx][2]
            local_map[idx] = [(mean_x_sq - mean**2)**(.5), mean]
        serialized = json.dumps(local_map)
        s3_client.put_object(Bucket=s3_bucket_input,
                             Key=str(i) + "_final_bounds", Body=serialized)
        s3_obj.delete()
