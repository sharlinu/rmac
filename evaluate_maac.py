import argparse

import gym
import torch
import time
from pathlib import Path
from torch.autograd import Variable
from algorithms.attention_sac import AttentionSAC, RelationalSAC
import os
import json
import yaml
import matplotlib.pyplot as plt
# from envs.env_wrappers import GraphDummyVecEnv, GraphSubprocVecEnv
from multiagent.MPE_env import MPEEnv, GraphMPEEnv
import numpy as np

def run(config):
    display = False
    model_path = config.model_path

    # create folder for evaluating
    eval_path = Path(config.model_path)
    eval_path = Path(*eval_path.parts[:-2])
    eval_path = '{}/evaluate'.format(eval_path)
    os.makedirs(eval_path, exist_ok=True)

    gif_path = '{}/{}'.format(eval_path, 'gifs')
    os.makedirs(gif_path, exist_ok=True)

    if config.alg == 'MAAC':
        model, _ = AttentionSAC.init_from_save(model_path, device=config.device)
    else:
        model, _ = RelationalSAC.init_from_save(model_path, device=config.device)
    print(config.env_id)
    if 'boxworld' in config.env_id:
        from environments.box import BoxWorldEnv
        env = BoxWorldEnv(
            players=config.player,
            field_size=(config.field,config.field),
            num_colours=config.num_colours,
            goal_length=config.goal_length,
            sight=config.field,
            max_episode_steps=config.test_episode_length,
            grid_observation=config.grid_observation,
            simple=config.simple,
            single=config.single,
            deterministic=config.deterministic,
        )
    elif 'lbf' in  config.env_id:
        from lbforaging.foraging import ForagingEnv
        env = ForagingEnv(
            players=config.player,
            # max_player_level=args.max_player_level,
            max_player_level=2,
            field_size=(config.field, config.field),
            max_food=config.lbf['max_food'],
            grid_observation=False,
            sight=config.field,
            max_episode_steps=50,
            force_coop=config.lbf['force_coop'],
            keep_food=config.lbf['keep_food'],
            simple=False,
        )
    elif 'push' in config.env_id:
        from bpush.environment import BoulderPush
        env = BoulderPush(
            width= config.field,
            height= config.field,
            n_agents=config.player,
            sensor_range=3,
        )
    elif 'wolf' in config.env_id:
        from Wolfpack_gym.envs.wolfpack import Wolfpack
        env = Wolfpack(
            grid_height=config.field,
            grid_width=config.field,
            num_players=config.player,
            max_food_num=config.wolfpack['max_food_num'],
            obs_type=config.wolfpack['obs_type'],
            sparse = config.wolfpack['sparse'],
            close_penalty = config.wolfpack['close_penalty'],
            # close_penalty = 0,
            )
    elif 'pp' in config.env_id:
        import macpp
        from utils.env_wrappers import FlatObs
        env=gym.make(f"macpp-{config.field}x{config.field}-{config.player}a-{config.pp['n_picker']}p-{config.pp['n_objects']}o-{config.pp['version']}",
                       debug_mode=False)
        env = FlatObs(env)
    elif 'MAPE' in config.env_id:
        # config.env_id =
        env = GraphMPEEnv(config)

    else:
        raise ValueError(f'Cannot cater for the environment {config.env_id}')

    model.prep_rollouts(device='cpu')
    ifi = 1 / config.fps  # inter-frame interval
    collect_data = {}
    l_ep_rew = []
    for ep_i in range(config.test_n_episodes):
        print("Episode %i of %i" % (ep_i + 1, config.test_n_episodes))

        frames = []
        # fig, ax = plt.subplots()
        collect_item = {
            'ep': ep_i,
            'final_reward': 0,
            'l_infos': [],
            'l_rewards': []
        }
        l_rewards = []
        ep_rew = 0

        if 'MAPE' in config.env_id:
            obs, _, _, _ = env.reset()
        else:
            obs = env.reset()
        # env.seed(1)
        if config.render:
            time.sleep(0.5)
            env.render()
        for t_i in range(config.episode_length):
        # for t_i in range(10):
            calc_start = time.time()

            # if config.no_render != False:
            #     frames.append(env.render(mode='rgb_array'))

        # rearrange observations to be per agent, and convert to torch Variable
            if config.grid_observation:
                obs = [np.expand_dims(ob['image'].flatten(), axis=0) for ob in obs]
            torch_obs = [Variable(torch.Tensor(obs[i]).view(1, -1),
                                  requires_grad=False)
                         for i in range(model.n_agents)]

            # get actions as torch Variables
            torch_actions = model.target_step(torch_obs)
            # convert actions to numpy arrays
            actions = [np.argmax(ac.data.numpy().flatten()) for ac in torch_actions]
            # print('actions',actions)
            # print(torch_actions)
            # print(actions)
            if 'MAPE' in config.env_id:
                actions = [ac.data.numpy().flatten() for ac in torch_actions]
                obs, agent_id, node_obs, adj, rewards, dones, infos = env.step(actions)
            else:
                obs, rewards, dones, infos = env.step(actions)
            # print('obs', obs)
            # print(obs[0])
            # print(obs[1])
            if config.render:
                time.sleep(0.5)
                env.render()
                # env.render(actions=actions)
            # else:
            #     time.sleep(0.5)
            collect_item['l_infos'].append(infos)

            calc_end = time.time()
            elapsed = calc_end - calc_start
            if config.render and (elapsed < ifi):
                time.sleep(ifi - elapsed)
            ep_rew += sum(rewards)

            if all(dones):
                collect_item['finished'] = 1
                break

        l_ep_rew.append(ep_rew)
        print("Reward: {}".format(ep_rew))

        collect_data[ep_i] = collect_item
        with open('{}/collected_data.json'.format(eval_path), 'w') as outfile:
            json.dump(collect_data, outfile, indent=4)
    print("Average reward: {}".format(sum(l_ep_rew)/config.test_n_episodes))
    env.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("model_path", help="model_path")
    parser.add_argument("--save_gifs", action="store_true",
                        help="Saves gif of each episode into model directory")
    parser.add_argument("--incremental", default=None, type=int,
                        help="Load incremental policy from given episode " +
                             "rather than final policy")
    parser.add_argument("--test_n_episodes", default=10, type=int)
    parser.add_argument("--test_episode_length", default=50, type=int)
    parser.add_argument("--fps", default=30, type=int)
    parser.add_argument("--render", default=False, action="store_true",
                        help="render")
    parser.add_argument("--benchmark", action="store_false",
                        help="benchmark mode")
    config = parser.parse_args()

    args = vars(config)
    eval_path = Path(config.model_path)
    dir_exp = Path(*eval_path.parts[:-2])
    with open(f"{dir_exp}/config.yaml", "r") as file:
        params = yaml.load(file, Loader=yaml.FullLoader)

    for k , v in params.items():
        args[k] = v

    config.scenario_name = 'navigation_graph'
    config.num_agents: int = 7
    config.world_size = 2
    config.num_scripted_agents = 0
    config.num_obstacles: int = 3
    config.collaborative: bool = False
    config.max_speed: float = 2
    config.collision_rew: float = 5
    config.goal_rew: float = 5
    config.min_dist_thresh: float = 0.1
    config.use_dones: bool = False
    config.episode_length: int = 25
    config.max_edge_dist: float = 1
    config.graph_feat_type: str = "rgcn"
    config.env_name ='GraphMPE'
    config.seed = 4001
    config.device = 'cpu'
    config.grid_observation = False

    run(config)