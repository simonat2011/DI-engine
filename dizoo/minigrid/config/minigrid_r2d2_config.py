from easydict import EasyDict
from ding.entry import serial_pipeline
collector_env_num = 8
evaluator_env_num = 5
minigrid_r2d2_config = dict(
    exp_name='debug_minigrid_doorkey_r2d2_n5_bs2_ul40',
    env=dict(
        collector_env_num=collector_env_num,
        evaluator_env_num=evaluator_env_num,
        # env_id='MiniGrid-Empty-8x8-v0',
        # env_id='MiniGrid-FourRooms-v0',
        env_id='MiniGrid-DoorKey-16x16-v0',
        n_evaluator_episode=5,
        stop_value=0.96,
    ),
    policy=dict(
        cuda=True,
        on_policy=False,
        priority=True,
        priority_IS_weight=True,
        model=dict(
            obs_shape=2739,
            action_shape=7,
            encoder_hidden_size_list=[128, 128, 512],

        ),
        discount_factor=0.997,
        burnin_step=2,  # TODO(pu) 20
        nstep=5,
        # (int) the whole sequence length to unroll the RNN network minus
        # the timesteps of burnin part,
        # i.e., <the whole sequence length> = <burnin_step> + <unroll_len>
        unroll_len=40,  # TODO(pu) 80
        learn=dict(
            # according to the R2D2 paper, actor parameter update interval is 400
            # environment timesteps, and in per collect phase, we collect 32 sequence
            # samples, the length of each samlpe sequence is <burnin_step> + <unroll_len>,
            # which is 100 in our seeting, 32*100/400=8, so we set update_per_collect=8
            # in most environments
            update_per_collect=8,
            batch_size=64,
            learning_rate=0.0005,
            target_update_theta=0.001,
        ),
        collect=dict(
            # NOTE it is important that don't include key n_sample here, to make sure self._traj_len=INF
            each_iter_n_sample=32,
            env_num=collector_env_num,
        ),
        eval=dict(env_num=evaluator_env_num, ),
        other=dict(
            eps=dict(
                type='exp',
                start=0.95,
                end=0.05,
                decay=1e5,
            ), 
            replay_buffer=dict(replay_buffer_size=100000,
                            # (Float type) How much prioritization is used: 0 means no prioritization while 1 means full prioritization
                            alpha=0.6,
                            # (Float type)  How much correction is used: 0 means no correction while 1 means full correction
                            beta=0.4,
                            )
        ),
    ),
)
minigrid_r2d2_config = EasyDict(minigrid_r2d2_config)
main_config = minigrid_r2d2_config
minigrid_r2d2_create_config = dict(
    env=dict(
        type='minigrid',
        import_names=['dizoo.minigrid.envs.minigrid_env'],
    ),
    env_manager=dict(type='base'),
    policy=dict(type='r2d2'),
)
minigrid_r2d2_create_config = EasyDict(minigrid_r2d2_create_config)
create_config = minigrid_r2d2_create_config

if __name__ == "__main__":
    serial_pipeline([main_config, create_config], seed=0)
