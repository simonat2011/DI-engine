from typing import List, Dict, Any, Tuple
from collections import namedtuple
import copy
import torch

from ding.torch_utils import Adam, to_device
from ding.rl_utils import q_nstep_td_data, q_nstep_td_error, get_nstep_return_data, get_train_sample
from ding.model import model_wrap
from ding.utils import POLICY_REGISTRY
from ding.utils.data import default_collate, default_decollate
from .base_policy import Policy
from .common_utils import default_preprocess_learn


@POLICY_REGISTRY.register('dqn')
class DQNPolicy(Policy):
    r"""
    Overview:
        Policy class of DQN algorithm, extended by Double DQN/Dueling DQN/PER/multi-step TD.

    Config:
        == ==================== ======== ============== ======================================== =======================
        ID Symbol               Type     Default Value  Description                              Other(Shape)
        == ==================== ======== ============== ======================================== =======================
        1  ``type``             str      dqn            | RL policy register name, refer to      | This arg is optional,
                                                        | registry ``POLICY_REGISTRY``           | a placeholder
        2  ``cuda``             bool     False          | Whether to use cuda for network        | This arg can be diff-
                                                                                                 | erent from modes
        3  ``on_policy``        bool     False          | Whether the RL algorithm is on-policy
                                                        | or off-policy
        4  ``priority``         bool     False          | Whether use priority(PER)              | Priority sample,
                                                                                                 | update priority
        5  | ``priority_IS``    bool     False          | Whether use Importance Sampling Weight
           | ``_weight``                                | to correct biased update. If True,
                                                        | priority must be True.
        6  | ``discount_``      float    0.97,          | Reward's future discount factor, aka.  | May be 1 when sparse
           | ``factor``                  [0.95, 0.999]  | gamma                                  | reward env
        7  ``nstep``            int      1,             | N-step reward discount sum for target
                                         [3, 5]         | q_value estimation
        8  | ``learn.update``   int      3              | How many updates(iterations) to train  | This args can be vary
           | ``per_collect``                            | after collector's one collection. Only | from envs. Bigger val
                                                        | valid in serial training               | means more off-policy
        9  | ``learn.batch_``   int      64             | The number of samples of an iteration
           | ``size``
        10 | ``learn.learning`` float    0.001          | Gradient step length of an iteration.
           | ``_rate``
        11 | ``learn.target_``  int      100            | Frequence of target network update.    | Hard(assign) update
           | ``update_freq``
        12 | ``learn.ignore_``  bool     False          | Whether ignore done for target value   | Enable it for some
           | ``done``                                   | calculation.                           | fake termination env
        13 ``collect.n_sample`` int      [8, 128]       | The number of training samples of a    | It varies from
                                                        | call of collector.                     | different envs
        14 | ``collect.unroll`` int      1              | unroll length of an iteration          | In RNN, unroll_len>1
           | ``_len``
        == ==================== ======== ============== ======================================== =======================
    """

    config = dict(
        type='dqn',
        cuda=False,
        on_policy=False,
        priority=False,
        # (bool) Whether use Importance Sampling Weight to correct biased update. If True, priority must be True.
        priority_IS_weight=False,
        discount_factor=0.97,
        nstep=1,
        learn=dict(
            # (bool) Whether to use multi gpu
            multi_gpu=False,
            # How many updates(iterations) to train after collector's one collection.
            # Bigger "update_per_collect" means bigger off-policy.
            # collect data -> update policy-> collect data -> ...
            update_per_collect=3,
            batch_size=64,
            learning_rate=0.001,
            # ==============================================================
            # The following configs are algorithm-specific
            # ==============================================================
            # (int) Frequence of target network update.
            target_update_freq=100,
            # (bool) Whether ignore done(usually for max step termination env)
            ignore_done=False,
        ),
        # collect_mode config
        collect=dict(
            # (int) Only one of [n_sample, n_episode] shoule be set
            # n_sample=8,
            # (int) Cut trajectories into pieces with length "unroll_len".
            unroll_len=1,
        ),
        eval=dict(),
        # other config
        other=dict(
            # Epsilon greedy with decay.
            eps=dict(
                # (str) Decay type. Support ['exp', 'linear'].
                type='exp',
                start=0.95,
                end=0.1,
                # (int) Decay length(env step)
                decay=10000,
            ),
            replay_buffer=dict(replay_buffer_size=10000, ),
        ),
    )

    def _init_learn(self) -> None:
        """
        Overview:
            Learn mode init method. Called by ``self.__init__``, initialize the optimizer, algorithm arguments, main \
            and target model.
        """
        self._priority = self._cfg.priority
        self._priority_IS_weight = self._cfg.priority_IS_weight
        # Optimizer
        self._optimizer = Adam(self._model.parameters(), lr=self._cfg.learn.learning_rate)

        self._gamma = self._cfg.discount_factor
        self._nstep = self._cfg.nstep

        # use model_wrapper for specialized demands of different modes
        self._target_model = copy.deepcopy(self._model)
        self._target_model = model_wrap(
            self._target_model,
            wrapper_name='target',
            update_type='assign',
            update_kwargs={'freq': self._cfg.learn.target_update_freq}
        )
        self._learn_model = model_wrap(self._model, wrapper_name='argmax_sample')
        self._learn_model.reset()
        self._target_model.reset()

    def _forward_learn(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Overview:
            Forward computation graph of learn mode(updating policy).
        Arguments:
            - data (:obj:`Dict[str, Any]`): Dict type data, a batch of data for training, values are torch.Tensor or \
                np.ndarray or dict/list combinations.
        Returns:
            - info_dict (:obj:`Dict[str, Any]`): Dict type data, a info dict indicated training result, which will be \
                recorded in text log and tensorboard, values are python scalar or a list of scalars.
        ArgumentsKeys:
            - necessary: ``obs``, ``action``, ``reward``, ``next_obs``, ``done``
            - optional: ``value_gamma``, ``IS``
        ReturnsKeys:
            - necessary: ``cur_lr``, ``total_loss``, ``priority``
            - optional: ``action_distribution``
        """
        data = default_preprocess_learn(
            data,
            use_priority=self._priority,
            use_priority_IS_weight=self._cfg.priority_IS_weight,
            ignore_done=self._cfg.learn.ignore_done,
            use_nstep=True
        )
        if self._cuda:
            data = to_device(data, self._device)
        # ====================
        # Q-learning forward
        # ====================
        self._learn_model.train()
        self._target_model.train()
        # Current q value (main model)
        q_value = self._learn_model.forward(data['obs'])['logit']
        # Target q value
        with torch.no_grad():
            target_q_value = self._target_model.forward(data['next_obs'])['logit']
            # Max q value action (main model)
            target_q_action = self._learn_model.forward(data['next_obs'])['action']

        data_n = q_nstep_td_data(
            q_value, target_q_value, data['action'], target_q_action, data['reward'], data['done'], data['weight']
        )
        value_gamma = data.get('value_gamma')
        loss, td_error_per_sample = q_nstep_td_error(data_n, self._gamma, nstep=self._nstep, value_gamma=value_gamma)

        # ====================
        # Q-learning update
        # ====================
        self._optimizer.zero_grad()
        loss.backward()
        if self._cfg.learn.multi_gpu:
            self.sync_gradients(self._learn_model)
        self._optimizer.step()

        # =============
        # after update
        # =============
        self._target_model.update(self._learn_model.state_dict())
        return {
            'cur_lr': self._optimizer.defaults['lr'],
            'total_loss': loss.item(),
            'q_value': q_value.mean().item(),
            'priority': td_error_per_sample.abs().tolist(),
            # Only discrete action satisfying len(data['action'])==1 can return this and draw histogram on tensorboard.
            # '[histogram]action_distribution': data['action'],
        }

    def _monitor_vars_learn(self) -> List[str]:
        return ['cur_lr', 'total_loss', 'q_value']

    def _state_dict_learn(self) -> Dict[str, Any]:
        """
        Overview:
            Return the state_dict of learn mode, usually including model and optimizer.
        Returns:
            - state_dict (:obj:`Dict[str, Any]`): the dict of current policy learn state, for saving and restoring.
        """
        return {
            'model': self._learn_model.state_dict(),
            'target_model': self._target_model.state_dict(),
            'optimizer': self._optimizer.state_dict(),
        }

    def _load_state_dict_learn(self, state_dict: Dict[str, Any]) -> None:
        """
        Overview:
            Load the state_dict variable into policy learn mode.
        Arguments:
            - state_dict (:obj:`Dict[str, Any]`): the dict of policy learn state saved before.

        .. tip::
            If you want to only load some parts of model, you can simply set the ``strict`` argument in \
            load_state_dict to ``False``, or refer to ``ding.torch_utils.checkpoint_helper`` for more \
            complicated operation.
        """
        self._learn_model.load_state_dict(state_dict['model'])
        self._target_model.load_state_dict(state_dict['target_model'])
        self._optimizer.load_state_dict(state_dict['optimizer'])

    def _init_collect(self) -> None:
        """
        Overview:
            Collect mode init method. Called by ``self.__init__``, initialize algorithm arguments and collect_model, \
            enable the eps_greedy_sample for exploration.
        """
        self._unroll_len = self._cfg.collect.unroll_len
        self._gamma = self._cfg.discount_factor  # necessary for parallel
        self._nstep = self._cfg.nstep  # necessary for parallel
        self._collect_model = model_wrap(self._model, wrapper_name='eps_greedy_sample')
        self._collect_model.reset()

    def _forward_collect(self, data: Dict[int, Any], eps: float) -> Dict[int, Any]:
        """
        Overview:
            Forward computation graph of collect mode(collect training data), with eps_greedy for exploration.
        Arguments:
            - data (:obj:`Dict[str, Any]`): Dict type data, stacked env data for predicting policy_output(action), \
                values are torch.Tensor or np.ndarray or dict/list combinations, keys are env_id indicated by integer.
            - eps (:obj:`float`): epsilon value for exploration, which is decayed by collected env step.
        Returns:
            - output (:obj:`Dict[int, Any]`): The dict of predicting policy_output(action) for the interaction with \
                env and the constructing of transition.
        ArgumentsKeys:
            - necessary: ``obs``
        ReturnsKeys
            - necessary: ``logit``, ``action``
        """
        data_id = list(data.keys())
        data = default_collate(list(data.values()))
        if self._cuda:
            data = to_device(data, self._device)
        self._collect_model.eval()
        with torch.no_grad():
            output = self._collect_model.forward(data, eps=eps)
        if self._cuda:
            output = to_device(output, 'cpu')
        output = default_decollate(output)
        return {i: d for i, d in zip(data_id, output)}

    def _get_train_sample(self, data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Overview:
            For a given trajectory(transitions, a list of transition) data, process it into a list of sample that \
            can be used for training directly. A train sample can be a processed transition(DQN with nstep TD) \
            or some continuous transitions(DRQN).
        Arguments:
            - data (:obj:`List[Dict[str, Any]`): The trajectory data(a list of transition), each element is the same \
                format as the return value of ``self._process_transition`` method.
        Returns:
            - samples (:obj:`dict`): The list of training samples.

        .. note::
            We will vectorize ``process_transition`` and ``get_train_sample`` method in the following release version. \
            And the user can customize the this data processing procecure by overriding this two methods and collector \
            itself.
        """
        data = get_nstep_return_data(data, self._nstep, gamma=self._gamma)
        return get_train_sample(data, self._unroll_len)

    def _process_transition(self, obs: Any, policy_output: Dict[str, Any], timestep: namedtuple) -> Dict[str, Any]:
        """
        Overview:
            Generate a transition(e.g.: <s, a, s', r, d>) for this algorithm training.
        Arguments:
            - obs (:obj:`Any`): Env observation.
            - policy_output (:obj:`Dict[str, Any]`): The output of policy collect mode(``self._forward_collect``),\
                including at least ``action``.
            - timestep (:obj:`namedtuple`): The output after env step(execute policy output action), including at \
                least ``obs``, ``reward``, ``done``, (here obs indicates obs after env step).
        Returns:
            - transition (:obj:`dict`): Dict type transition data.
        """
        transition = {
            'obs': obs,
            'next_obs': timestep.obs,
            'action': policy_output['action'],
            'reward': timestep.reward,
            'done': timestep.done,
        }
        return transition

    def _init_eval(self) -> None:
        r"""
        Overview:
            Evaluate mode init method. Called by ``self.__init__``, initialize eval_model.
        """
        self._eval_model = model_wrap(self._model, wrapper_name='argmax_sample')
        self._eval_model.reset()

    def _forward_eval(self, data: Dict[int, Any]) -> Dict[int, Any]:
        """
        Overview:
            Forward computation graph of eval mode(evaluate policy performance), at most cases, it is similar to \
            ``self._forward_collect``.
        Arguments:
            - data (:obj:`Dict[str, Any]`): Dict type data, stacked env data for predicting policy_output(action), \
                values are torch.Tensor or np.ndarray or dict/list combinations, keys are env_id indicated by integer.
        Returns:
            - output (:obj:`Dict[int, Any]`): The dict of predicting action for the interaction with env.
        ArgumentsKeys:
            - necessary: ``obs``
        ReturnsKeys
            - necessary: ``action``
        """
        data_id = list(data.keys())
        data = default_collate(list(data.values()))
        if self._cuda:
            data = to_device(data, self._device)
        self._eval_model.eval()
        with torch.no_grad():
            output = self._eval_model.forward(data)
        if self._cuda:
            output = to_device(output, 'cpu')
        output = default_decollate(output)
        return {i: d for i, d in zip(data_id, output)}

    def default_model(self) -> Tuple[str, List[str]]:
        """
        Overview:
            Return this algorithm default model setting for demonstration.
        Returns:
            - model_info (:obj:`Tuple[str, List[str]]`): model name and mode import_names

        .. note::
            The user can define and use customized network model but must obey the same inferface definition indicated \
            by import_names path. For DQN, ``ding.model.template.q_learning.DQN``
        """
        return 'dqn', ['ding.model.template.q_learning']
