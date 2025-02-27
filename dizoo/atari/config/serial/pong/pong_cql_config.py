from copy import deepcopy
from ding.entry import serial_pipeline, serial_pipeline_offline
from easydict import EasyDict

pong_cql_config = dict(
    env=dict(
        collector_env_num=1,
        evaluator_env_num=8,
        n_evaluator_episode=8,
        stop_value=20,
        env_id='PongNoFrameskip-v4',
        frame_stack=4,
        manager=dict(shared_memory=False, )
    ),
    policy=dict(
        cuda=True,
        priority=False,
        model=dict(
            obs_shape=[4, 84, 84],
            action_shape=6,
            encoder_hidden_size_list=[128, 128, 512],
            num_quantiles=200,
        ),
        nstep=1,
        discount_factor=0.99,
        learn=dict(
            train_epoch=30000,
            batch_size=32,
            learning_rate=0.00005,
            target_update_freq=2000,
            min_q_weight=10.0,
        ),
        collect=dict(
            n_sample=100,
            data_type='hdf5',
            data_path='./default_experiment/expert.pkl',
        ),
        eval=dict(evaluator=dict(eval_freq=4000, )),
        other=dict(
            eps=dict(
                type='exp',
                start=1.,
                end=0.05,
                decay=250000,
            ),
            replay_buffer=dict(replay_buffer_size=100000, ),
        ),
    ),
)
pong_cql_config = EasyDict(pong_cql_config)
main_config = pong_cql_config
pong_cql_create_config = dict(
    env=dict(
        type='atari',
        import_names=['dizoo.atari.envs.atari_env'],
    ),
    env_manager=dict(type='base'),
    policy=dict(type='cql_discrete'),
)
pong_cql_create_config = EasyDict(pong_cql_create_config)
create_config = pong_cql_create_config

if __name__ == '__main__':
    serial_pipeline_offline((main_config, create_config), seed=0)
