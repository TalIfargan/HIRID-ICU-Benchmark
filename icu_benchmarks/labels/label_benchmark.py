""" Label generation from the benchmark endpoints"""

import gc
import glob
import logging
import os
import os.path
import pickle

import numpy as np
import pandas as pd

import icu_benchmarks.labels.utils as utils
from icu_benchmarks.common.constants import PID, MORTALITY_NAME, CIRC_FAILURE_NAME, RESP_FAILURE_NAME, URINE_REG_NAME, \
    URINE_BINARY_NAME, PHENOTYPING_NAME, LOS_NAME, STEPS_PER_HOUR, DATETIME

HR_CUM_NAME = 'vm1_IMPUTED_STATUS_CUM_COUNT'
URINE_CUM_NAME = 'vm24_IMPUTED_STATUS_CUM_COUNT'
APACHE_2_NAME = 'APACHE II Group'
APACHE_4_NAME = 'APACHE IV Group'
DISCHARGE_NAME = 'discharge_status'


def load_pickle(fpath):
    """ Given a file path pointing to a pickle file, yields the object pickled in this file"""
    with open(fpath, 'rb') as fp:
        return pickle.load(fp)


def delete_if_exist(path):
    """ Deletes a path if it exists on the file-system"""
    if os.path.exists(path):
        os.remove(path)


def create_dir_if_not_exist(path, recursive=False):
    """ Creates a directory if it does not yet exist in the file system"""
    if not os.path.exists(path):
        if recursive:
            os.makedirs(path)
        else:
            os.mkdir(path)


def is_df_sorted(df, colname):
    return (np.array(df[colname].diff().dropna(), dtype=np.float64) >= 0).all()


def gen_label(df_pat, df_endpoint, mort_status=None, apache_group=None, pid=None, configs=None):
    """Returns data-frame with label from patient input data-frames"""

    abs_time_col = df_pat[configs["abs_datetime_key"]]
    rel_time_col = df_pat[configs["rel_datetime_key"]]
    patient_col = df_pat[configs["patient_id_key"]]
    stay_length = len(rel_time_col)

    hr_col = np.array(df_pat[HR_CUM_NAME])
    hr_status_arr = utils.get_hr_status(hr_col)

    if df_pat.shape[0] == 0 or df_endpoint.shape[0] == 0:
        logging.info("WARNING: Patient {} has no impute data, skipping...".format(pid), flush=True)
        return None

    df_endpoint.set_index(keys=DATETIME, inplace=True, verify_integrity=True)
    assert ((df_pat.datetime == df_endpoint.index).all())

    output_df_dict = {}
    output_df_dict[configs["abs_datetime_key"]] = abs_time_col
    output_df_dict[configs["rel_datetime_key"]] = rel_time_col
    output_df_dict[configs["patient_id_key"]] = patient_col

    # Mortality, predicted after the first 24h
    dynamic_mort_arr = utils.unique_label_at_hours(stay_length, mort_status, at_hours=24)
    dynamic_mort_arr = utils.convolve_hr(dynamic_mort_arr, hr_status_arr)
    output_df_dict[MORTALITY_NAME] = dynamic_mort_arr

    # Circulatory Failure, predicted every 5min
    circ_failure_col = np.array(df_endpoint.circ_failure_status)
    dynamic_circ_failure = utils.transition_to_failure(circ_failure_col, lhours=0, rhours=12)
    dynamic_circ_failure = utils.convolve_hr(dynamic_circ_failure, hr_status_arr)
    output_df_dict[CIRC_FAILURE_NAME] = dynamic_circ_failure

    # Respiratory Failure, predicted every 5min
    pre_resp_arr = df_endpoint.resp_failure_status.values
    ann_resp_arr = utils.get_any_resp_label(pre_resp_arr)
    dynamic_resp_failure = utils.transition_to_failure(ann_resp_arr, lhours=0, rhours=12)
    dynamic_resp_failure = utils.convolve_hr(dynamic_resp_failure, hr_status_arr)
    output_df_dict[RESP_FAILURE_NAME] = dynamic_resp_failure

    # Urine in the next 2h, (Cont. regression) or (Binary below 0.5)
    weight_col = np.array(df_pat.vm131)
    urine_col = np.array(df_pat.vm24)
    urine_meas_arr = np.array(df_pat[URINE_CUM_NAME])
    urine_reg_arr, urine_binary_arr = utils.future_urine_output(urine_col, urine_meas_arr, weight_col, rhours=2)
    urine_reg_arr = utils.convolve_hr(urine_reg_arr, hr_status_arr)
    urine_binary_arr = utils.convolve_hr(urine_binary_arr, hr_status_arr)
    output_df_dict[URINE_REG_NAME] = urine_reg_arr
    output_df_dict[URINE_BINARY_NAME] = urine_binary_arr

    # Apache Score Phenotyping, predicted after the first 24h
    apache_arr = utils.unique_label_at_hours(stay_length, apache_group, at_hours=24)
    apache_arr = utils.convolve_hr(apache_arr, hr_status_arr)
    output_df_dict[PHENOTYPING_NAME] = apache_arr

    # Remaining length of stay, (Cont. regression)
    rem_los = np.linspace(stay_length / STEPS_PER_HOUR, 0, num=stay_length)
    rem_los = utils.convolve_hr(rem_los, hr_status_arr)
    output_df_dict[LOS_NAME] = rem_los

    output_df = pd.DataFrame(output_df_dict)
    return output_df


def label_gen_benchmark(configs):
    """Creation of base labels directly defined on the imputed data / endpoints for one batch"""
    label_base_dir = configs["label_dir"]
    endpoint_base_dir = configs["endpoint_dir"]
    imputed_base_dir = configs["imputed_dir"]
    apache_ii_map = configs["APACHE_II_map"]
    apache_iv_map = configs["APACHE_IV_map"]
    batch_idx = configs["batch_idx"]
    df_static = pd.read_parquet(configs["general_data_table_path"])
    all_out_dfs = []

    if not configs["debug_mode"]:
        delete_if_exist(os.path.join(label_base_dir, "batch_{}.parquet".format(batch_idx)))

    patient_path = os.path.join(imputed_base_dir, "batch_{}.parquet".format(batch_idx))
    df_all_pats = pd.read_parquet(patient_path)
    all_pids = df_all_pats[PID].unique()
    logging.info("Number of selected PIDs: {}".format(len(all_pids)))

    cand_files = glob.glob(os.path.join(endpoint_base_dir, "batch_{}.parquet".format(batch_idx)))
    assert (len(cand_files) == 1)
    endpoint_path = cand_files[0]
    df_all_endpoints = pd.read_parquet(endpoint_path)
    if configs["verbose"]:
        logging.info("Number of patient IDs: {}".format(len(all_pids)))

    n_skipped_patients = 0
    for pidx, pid in enumerate(all_pids):

        try:
            mort_code = str(df_static[df_static[PID] == pid][DISCHARGE_NAME].values[0])
            mort_status = mort_code == "dead"
        except ValueError:
            mort_status = False
        except TypeError:
            mort_status = False

        apache_ii_group = float(df_static[df_static[PID] == pid][APACHE_2_NAME])
        apache_iv_group = float(df_static[df_static[PID] == pid][APACHE_4_NAME])
        apache_pat_group = utils.merge_apache_groups(apache_ii_group, apache_iv_group,
                                                     apache_ii_map, apache_iv_map)

        # Checks patient information is sufficient
        if not os.path.exists(patient_path):
            logging.info("WARNING: Patient {} does not exists, skipping...".format(pid))
            n_skipped_patients += 1
            continue

        try:
            df_endpoint = df_all_endpoints[df_all_endpoints[PID] == pid]
        except:
            logging.info("WARNING: Issue while reading endpoints of patient {}".format(pid))
            n_skipped_patients += 1
            continue

        df_pat = df_all_pats[df_all_pats[PID] == pid]

        if df_pat.shape[0] == 0 or df_endpoint.shape[0] == 0:
            if df_pat.shape[0] == 0:
                logging.info("WARNING: Empty endpoints", flush=True)
            else:
                logging.info("WARNING: Empty imputed data in patient {}".format(pid), flush=True)

            n_skipped_patients += 1
            continue

        if not is_df_sorted(df_endpoint, DATETIME):
            df_endpoint = df_endpoint.sort_values(by=DATETIME, kind="mergesort")

        # Generates labels for patient
        df_label = gen_label(df_pat, df_endpoint, mort_status=mort_status, apache_group=apache_pat_group, pid=pid,
                             configs=configs)

        if df_label is None:
            logging.info("WARNING: Label could not be created for PID: {}".format(pid))
            n_skipped_patients += 1
            continue

        assert (df_label.shape[0] == df_pat.shape[0])

        if not configs["debug_mode"]:
            all_out_dfs.append(df_label)

        gc.collect()

        if (pidx + 1) % 100 == 0 and configs["verbose"]:
            logging.info("Progress for batch {}: {:.2f} %".format(batch_idx, (pidx + 1) / len(all_pids) * 100))
            logging.info("Number of skipped patients: {}".format(n_skipped_patients))

    if not configs["debug_mode"]:
        combined_df = pd.concat(all_out_dfs, axis=0)
        output_path = os.path.join(label_base_dir, "batch_{}.parquet".format(batch_idx))
        combined_df.to_parquet(output_path)
