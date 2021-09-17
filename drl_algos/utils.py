import datetime
import os
import os.path as osp
from collections import OrderedDict
from numbers import Number
import json
import dateutil.tz

import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np

from drl_algos.data.logging import logger
from drl_algos.data import conf


"""
Notes on Initialisation
The paper "HOW TO MAKE DEEP RL WORK IN PRACTICE", explored a number of
implementation choices, among them intilisations functions. The long and short
of it was that orthogonal seemed best for SAC and TD3, which uses ReLU
activations. Orthogonal, in theory, can be applied to any activation function
and it seems pretty good. This paper is nice because there aren't alot like it
in general, but especially for reinforcement learning.

The work focused on the effect of weight initilisation on early exploration,
finding that weight initialisation greatly changes the probability of sampling
different actions. A good weight initialisation scheme should have good coverage
of the action space whilst avoiding clipping at the extreme values (which causes
those actions to be oversampled).

Unlike most implementations I see, this work doesn't initialise the output layer
any differently than the hidden layers. Normally, the final layer is initialised
uniformally in a small range, e.g. 1e-3 in this implementation. The authors
didn't seem to compare to that scenario although I would suspect it should
explore worse. They mainly focused on the effect on policy exploration but it
seems they also initialised the critic with the same scheme. I would suspect
small uniform initilisation to be worse than orthogonal for early exploration but
I'm not sure about the critic. How they initialise the standard deviation layer
is confusing, they claim it was initialised to 1 but that doesn't seem to be
true. It doesn't seem learned so might need more experimenting.

To wrap up:
    - Orthogonal seems good, but should test final layer initialisation as the
      small initialisation is done for stability reasons but this seems to have
      been ignored in paper experiments.
    - I should do experiments on pendulum and maybe walker2d to compare fanin
      and orthogonal in the following scenarios:
      - Initialise every layer but small init for output layers (current approach using fanin)
      - Initialise every layer but small init for critic and policy std output
        layers (highest probability of being helpful)
        - This may boost initial exploration
        - May want to try initialising policy mean layer for tanh instead of
          ReLU
      - Initialise every layer but small init for policy std output only (and mean layer if above doesn't help)
        - I suspect critic initialisation will not help
        - May want to initialise for linear instead of relu
      - Initialise every layer (except mean and critic if they don't help)
        - I suspect std initialisation doesn't help but maybe
        - Again, may want to initialise for tanh
      - Initialise every layer (baseline if any of above don't prove helpful)
        - May want to try initialising final layers for correct activation
    - I suspect orthogonal and/or kaiming will work well for Dreamer
      - It has less concerns about final layer initialisation because its
        supervised so no need to run above experiments
      - May want to initialise the final layers for their specific output
        activation
        - I'm not sure what elu's is, probably just use relu for gain
        - Could try replace elu with relu and see if it works better with the
          initialisation
"""


def filter_activation_name(activation):
    """This function replaces the activation name with relu if it is not
    supported for initialisation.

    Many initialisation functions take gain terms or the name of the activation
    as input. Gains can be calculated using torch's built-in function but it does
    not support all activations. I assume functions taking names instead of
    gains only support the same activations which seems to be the case with
    kaiming. Note - it seems like newer torch versions also support selu and
    have added an identity activation.

    If wishing to extend to new activations, it may be best to develop our own
    calculate gains function so we can support more activations.
    """
    if activation in [F.linear, F.conv1d, F.conv2d, F.conv3d, F.sigmoid, F.tanh,
                      F.relu, F.leaky_relu]:
        return activation.__name__
    return "relu"


def initialise(weight_tensor, function="fanin", activation=F.relu):
    name = filter_activation_name(activation)
    if function == "fanin":
        fanin(weight_tensor)
    elif function == "xavier":
        xavier(weight_tensor, name)
    elif function == "kaiming":
        kaiming(weight_tensor, name)
    elif function == "orthogonal":
        orthogonal(weight_tensor, name)


def fanin(tensor):
    """Default initialisation common in reinforcement learning."""
    fan_in, _ = nn.init._calculate_fan_in_and_fan_out(tensor)
    bound = 1. / np.sqrt(fan_in)
    nn.init.uniform_(tensor, a=-bound, b=bound)


def xavier(tensor, activation='tanh'):
    """Common initialisation function, normally used with tanh."""
    nn.init.xavier_uniform_(tensor, gain=nn.init.calculate_gain(activation))


def kaiming(tensor, activation='relu'):
    """Common initialisation function, normally used with relu or leaky relu."""
    nn.init.kaiming_uniform_(tensor, nonlinearity=activation)


def orthogonal(tensor, activation='relu'):
    """Initilisation function that can be applied to any activation.

    Has some interesting theoretical properties and seems to be attracting more
    attention. Seems to be good for SAC and TD3 with ReLU activations. Note -
    seems like they actually used gain for Tanh instead of RelU.
    """
    # TODO - compare always using tahn gain
    nn.init.orthogonal_(tensor, gain=nn.init.calculate_gain(activation))


def count_parameters(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


def to_tensor(np_array, device="cpu"):
    if isinstance(np_array, np.ndarray):
        return torch.from_numpy(np_array).float().to(device)
    return np_array.float().to(device)


def to_numpy(data):
    if isinstance(data, tuple):
        return tuple(to_numpy(x) for x in data)
    if isinstance(data, torch.autograd.Variable):
        return data.to('cpu').detach().numpy()
    return data


def to_tensor_batch(batch, device="cpu"):
    if isinstance(batch, dict):
        return {
            k: to_tensor(x, device)
            for k, x in _filter_batch(batch)
            if x.dtype != np.dtype('O')  # ignore object (e.g. dictionaries)
        }
    else:
        to_tensor(batch, device)


def cat(data, dim=1):
    if isinstance(data[0], torch.autograd.Variable):
        return torch.cat(data, dim=dim)
    return np.concatenate(data, axis=dim)


def identity(x):
    return x


def soft_update(source, target, tau):
    target_params = target.parameters()
    source_params = source.parameters()
    for target_param, source_param in zip(target_params, source_params):
        new_param = (target_param.data * (1.0 - tau)
                     + source_param.data * tau)
        target_param.data.copy_(new_param)


def cat_dict(orig, new):
    for key in new.keys():
        if key in orig.keys():
            orig[key].append(new[key])
        else:
            orig[key] = [new[key]]


def add_dict(orig, new, prefix=""):
    if prefix:
        if not prefix.endswith("/"):
            prefix += "/"
    for key in new.keys():
        orig[prefix+key] = new[key]


def add_prefix(log_dict, prefix, divider=''):
    with_prefix = {}
    for key, val in log_dict.items():
        with_prefix[prefix + divider + key] = val
    return with_prefix


def _filter_batch(batch):
    for k, v in batch.items():
        if v.dtype == np.bool:
            yield k, v.astype(int)
        else:
            yield k, v


def create_stats_ordered_dict(
        name,
        data,
        stat_prefix=None,
        always_show_all_stats=True,
        exclude_max_min=False,
):
    if stat_prefix is not None:
        name = "{}{}".format(stat_prefix, name)

    if isinstance(data, Number):
        return OrderedDict({name: data})

    if len(data) == 0:
        return OrderedDict()

    if isinstance(data, tuple):
        ordered_dict = OrderedDict()
        for number, d in enumerate(data):
            sub_dict = create_stats_ordered_dict(
                "{0}_{1}".format(name, number),
                d,
            )
            ordered_dict.update(sub_dict)
        return ordered_dict

    if isinstance(data, list):
        try:
            iter(data[0])
        except TypeError:
            pass
        else:
            data = np.concatenate(data)

    if (isinstance(data, np.ndarray) and data.size == 1
            and not always_show_all_stats):
        return OrderedDict({name: float(data)})

    stats = OrderedDict([
        (name + ' Mean', np.mean(data)),
        (name + ' Std', np.std(data)),
    ])
    if not exclude_max_min:
        stats[name + ' Max'] = np.max(data)
        stats[name + ' Min'] = np.min(data)
    return stats


def setup_logger(
        exp_prefix="default",
        variant=None,
        text_log_file="debug.log",
        variant_log_file="variant.json",
        tabular_log_file="progress.csv",
        snapshot_mode="last",
        snapshot_gap=1,
        log_tabular_only=False,
        log_dir=None,
        script_name=None,
        **create_log_dir_kwargs
):
    """
    Set up logger to have some reasonable default settings.
    Will save log output to
        based_log_dir/exp_prefix/exp_name.
    exp_name will be auto-generated to be unique.
    If log_dir is specified, then that directory is used as the output dir.
    :param exp_prefix: The sub-directory for this specific experiment.
    :param variant:
    :param text_log_file:
    :param variant_log_file:
    :param tabular_log_file:
    :param snapshot_mode:
    :param log_tabular_only:
    :param snapshot_gap:
    :param log_dir:
    :param script_name: If set, save the script name to this.
    :return:
    """
    first_time = log_dir is None
    if first_time:
        log_dir = create_log_dir(exp_prefix, **create_log_dir_kwargs)

    if variant is not None:
        logger.log("Variant:")
        logger.log(json.dumps(dict_to_safe_json(variant), indent=2))
        variant_log_path = osp.join(log_dir, variant_log_file)
        logger.log_variant(variant_log_path, variant)

    tabular_log_path = osp.join(log_dir, tabular_log_file)
    text_log_path = osp.join(log_dir, text_log_file)

    logger.add_text_output(text_log_path)
    if first_time:
        logger.add_tabular_output(tabular_log_path)
    else:
        logger._add_output(tabular_log_path, logger._tabular_outputs,
                           logger._tabular_fds, mode='a')
        for tabular_fd in logger._tabular_fds:
            logger._tabular_header_written.add(tabular_fd)
    logger.set_snapshot_dir(log_dir)
    logger.set_snapshot_mode(snapshot_mode)
    logger.set_snapshot_gap(snapshot_gap)
    logger.set_log_tabular_only(log_tabular_only)
    exp_name = log_dir.split("/")[-1]
    logger.push_prefix("[%s] " % exp_name)

    if script_name is not None:
        with open(osp.join(log_dir, "script_name.txt"), "w") as f:
            f.write(script_name)
    return log_dir


def create_log_dir(
        exp_prefix,
        exp_id=0,
        seed=0,
        base_log_dir=None,
        include_exp_prefix_sub_dir=True,
):
    """
    Creates and returns a unique log directory.
    :param exp_prefix: All experiments with this prefix will have log
    directories be under this directory.
    :param exp_id: The number of the specific experiment run within this
    experiment.
    :param base_log_dir: The directory where all log should be saved.
    :return:
    """
    exp_name = create_exp_name(exp_prefix, exp_id=exp_id,
                               seed=seed)
    if base_log_dir is None:
        base_log_dir = conf.LOCAL_LOG_DIR
    if include_exp_prefix_sub_dir:
        log_dir = osp.join(base_log_dir, exp_prefix.replace("_", "-"), exp_name)
    else:
        log_dir = osp.join(base_log_dir, exp_name)
    if osp.exists(log_dir):
        print("WARNING: Log directory already exists {}".format(log_dir))
    os.makedirs(log_dir, exist_ok=True)
    return log_dir


def create_exp_name(exp_prefix, exp_id=0, seed=0):
    """
    Create a semi-unique experiment name that has a timestamp
    :param exp_prefix:
    :param exp_id:
    :return:
    """
    now = datetime.datetime.now(dateutil.tz.tzlocal())
    timestamp = now.strftime('%Y_%m_%d_%H_%M_%S')
    return "%s_%s_%04d--s-%d" % (exp_prefix, timestamp, exp_id, seed)


def dict_to_safe_json(d):
    """
    Convert each value in the dictionary into a JSON'able primitive.
    :param d:
    :return:
    """
    new_d = {}
    for key, item in d.items():
        if safe_json(item):
            new_d[key] = item
        else:
            if isinstance(item, dict):
                new_d[key] = dict_to_safe_json(item)
            else:
                new_d[key] = str(item)
    return new_d


def safe_json(data):
    if data is None:
        return True
    elif isinstance(data, (bool, int, float)):
        return True
    elif isinstance(data, (tuple, list)):
        return all(safe_json(x) for x in data)
    elif isinstance(data, dict):
        return all(isinstance(k, str) and safe_json(v) for k, v in data.items())
    return False
