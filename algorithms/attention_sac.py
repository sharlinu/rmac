import torch
import torch.nn.functional as F
from torch.optim import Adam
from utils.misc import soft_update, hard_update, enable_gradients, disable_gradients
from utils.agents import AttentionAgent
from utils.critics import RelationalCritic
import numpy as np

MSELoss = torch.nn.MSELoss()

class RelationalSAC(object):
    """
    Wrapper class for SAC agents with central attention critic in multi-agent
    task
    """
    def __init__(self,
                 agent_init_params,
                 spatial_tensors,
                 batch_size,
                 n_actions,
                 input_dims,
                 n_agents=2,
                 gamma=0.95,
                 tau=0.01,
                 pi_lr=0.01,
                 q_lr=0.01,
                 reward_scale=10.,
                 pol_hidden_dim=128,
                 critic_hidden_dim=128,
                 **kwargs):
        """
        Inputs:
            agent_init_params (list of dict): List of dicts with parameters to
                                              initialize each agent, input size (observation shape) and output size (action shape)
                num_in_pol (int): Input dimensions to policy
                num_out_pol (int): Output dimensions to policy
            sa_size (list of (int, int)): Size of state and action space for
                                          each agent
            gamma (float): Discount factor
            tau (float): Target update rate
            pi_lr (float): Learning rate for policy
            q_lr (float): Learning rate for critic
            reward_scale (float): Scaling for reward (has effect of optimal
                                  policy entropy)
            hidden_dim (int): Number of hidden dimensions for networks
        """
        self.n_agents = n_agents
        self.agents = [AttentionAgent(lr=pi_lr,
                                      hidden_dim=pol_hidden_dim,
                                      num_in_pol=params['num_in_pol'],
                                      num_out_pol=params['num_out_pol'])
                         for params in agent_init_params] # are input and output dims for agent
        self.critic = RelationalCritic(n_agents=self.n_agents,
                                       # obs = obs,
                                       spatial_tensors=spatial_tensors,
                                       batch_size = batch_size,
                                       n_actions=n_actions,
                                       input_dims=input_dims,
                                       hidden_dim=critic_hidden_dim)
        self.target_critic = RelationalCritic(
                                        n_agents = self.n_agents,
                                        # obs = obs,
                                        spatial_tensors=spatial_tensors,
                                        batch_size = batch_size,
                                        n_actions=n_actions,
                                        input_dims=input_dims,
                                        hidden_dim=critic_hidden_dim)
        hard_update(self.target_critic, self.critic) # hard update only at the beginning to initialise
        self.critic_optimizer = Adam(self.critic.parameters(), lr=q_lr,
                                     weight_decay=1e-3)
        self.agent_init_params = agent_init_params #in: obs.shape out: action.shape
        self.gamma = gamma
        self.tau = tau
        self.pi_lr = pi_lr
        self.q_lr = q_lr
        self.reward_scale = reward_scale
        self.pol_dev = 'cpu'  # device for policies
        self.critic_dev = 'cpu'  # device for critics
        self.trgt_pol_dev = 'cpu'  # device for target policies
        self.trgt_critic_dev = 'cpu'  # device for target critics
        self.niter = 0

    @property
    def policies(self):
        return [a.policy for a in self.agents]

    @property
    def target_policies(self):
        return [a.target_policy for a in self.agents]

    def step(self, observations, explore=False):
        """
        Take a step forward in environment with all agents
        Inputs:
            observations: List of observations for each agent
        Outputs:
            actions: List of actions for each agent
        """
        # observations = [observations['image']] * 2
        return [a.step(obs, explore=explore) for a, obs in zip(self.agents,
                                                               observations)]

    def update_critic(self, sample, soft=True, logger=None, **kwargs):
        """
        Update central critic for all agents
        """
        obs, unary,binary, acs, rews, next_obs, next_unary, next_binary, dones = sample

        # Q loss
        next_acs = []
        next_log_pis = []
        for pi, ob in zip(self.target_policies, next_obs):
            curr_next_ac, curr_next_log_pi = pi(ob, return_log_pi=True)
            next_acs.append(curr_next_ac)
            next_log_pis.append(curr_next_log_pi)

        critic_rets = self.critic(obs=obs, unary_tensors=unary, binary_tensors=binary, actions=acs,
                                  logger=logger, niter=self.niter)
        next_qs = self.target_critic(obs=next_obs, unary_tensors=next_unary, binary_tensors=next_binary, actions=next_acs)
        q_loss = 0
        for a_i, nq, log_pi, pq in zip(range(self.n_agents), next_qs,
                                               next_log_pis, critic_rets):
            target_q = (rews[a_i].view(-1, 1) +
                        self.gamma * nq *
                        (1 - dones[a_i].view(-1, 1)))
            if soft:
                target_q -= log_pi / self.reward_scale
            q_loss += MSELoss(pq, target_q.detach())

        q_loss.backward()
        for n,p in self.critic.named_parameters():
            #if n[-6:] == 'weight':
            #print('===========\ngradient:{}\n----------\n{}'.format(n, p.grad))
            logger.add_scalar(f'grad/sum_{n}', p.grad.sum(), self.niter)
            logger.add_scalar(f'grad/mean_{n}', p.grad.mean(), self.niter)
            logger.add_scalar(f'weight/mean_{n}', p.mean(), self.niter)


        self.critic.scale_shared_grads()
        grad_norm = torch.nn.utils.clip_grad_norm_(
            self.critic.parameters(), 10 * self.n_agents)


        self.critic_optimizer.step()
        self.critic_optimizer.zero_grad()

        if logger is not None:
            logger.add_scalar('losses/q_loss', q_loss, self.niter)
            logger.add_scalar('grad_norms/q', grad_norm, self.niter)
        self.niter += 1

    def update_policies(self, sample, soft=True, logger=None, **kwargs):
        obs, unary, binary, acs, rews, next_obs, next_unary, next_binary,  dones = sample
        samp_acs = []
        all_probs = []
        all_log_pis = []
        all_pol_regs = []

        for a_i, pi, ob in zip(range(self.n_agents), self.policies, obs):
            curr_ac, probs, log_pi, pol_regs, ent = pi(
                ob, return_all_probs=True, return_log_pi=True,
                regularize=True, return_entropy=True)
            logger.add_scalar('agent%i/policy_entropy' % a_i, ent,
                              self.niter)
            samp_acs.append(curr_ac)
            all_probs.append(probs)
            all_log_pis.append(log_pi)
            all_pol_regs.append(pol_regs)

        critic_rets = self.critic(obs=obs, unary_tensors=unary, binary_tensors=binary, actions=samp_acs,
                                  logger=logger, return_all_q=True)

        for a_i, probs, log_pi, pol_regs, (q, all_q) in zip(range(self.n_agents), all_probs,
                                                            all_log_pis, all_pol_regs,
                                                            critic_rets):
            curr_agent = self.agents[a_i]
            v = (all_q * probs).sum(dim=1, keepdim=True)
            pol_target = q - v
            if soft:
                pol_loss = (log_pi * (log_pi / self.reward_scale - pol_target).detach()).mean()
            else:
                pol_loss = (log_pi * (-pol_target).detach()).mean()
            for reg in pol_regs:
                pol_loss += 1e-3 * reg  # policy regularization
            # don't want critic to accumulate gradients from policy loss
            disable_gradients(self.critic)
            pol_loss.backward()
            enable_gradients(self.critic)

            grad_norm = torch.nn.utils.clip_grad_norm_(
                curr_agent.policy.parameters(), 0.5)
            curr_agent.policy_optimizer.step()
            curr_agent.policy_optimizer.zero_grad()

            if logger is not None:
                logger.add_scalar('agent%i/losses/pol_loss' % a_i,
                                  pol_loss, self.niter)
                logger.add_scalar('agent%i/grad_norms/pi' % a_i,
                                  grad_norm, self.niter)


    def update_all_targets(self):
        """
        Update all target networks (called after normal updates have been
        performed for each agent)
        """
        soft_update(self.target_critic, self.critic, self.tau)
        for a in self.agents:
            soft_update(a.target_policy, a.policy, self.tau)

    def prep_training(self, device='gpu'):
        self.critic.train()
        self.target_critic.train()
        for a in self.agents:
            a.policy.train()
            a.target_policy.train()
        if device == 'gpu':
            fn = lambda x: x.cuda()
        else:
            fn = lambda x: x.cpu()
        if not self.pol_dev == device:
            for a in self.agents:
                a.policy = fn(a.policy)
            self.pol_dev = device
        if not self.critic_dev == device:
            self.critic = fn(self.critic)
            self.critic_dev = device
        if not self.trgt_pol_dev == device:
            for a in self.agents:
                a.target_policy = fn(a.target_policy)
            self.trgt_pol_dev = device
        if not self.trgt_critic_dev == device:
            self.target_critic = fn(self.target_critic)
            self.trgt_critic_dev = device

    def prep_rollouts(self, device='cpu'):
        for a in self.agents:
            a.policy.eval()
        if device == 'gpu':
            fn = lambda x: x.cuda()
        else:
            fn = lambda x: x.cpu()
        # only need main policy for rollouts
        if not self.pol_dev == device:
            for a in self.agents:
                a.policy = fn(a.policy)
            self.pol_dev = device

    def save(self, filename, episode = None):
        """
        Save trained parameters of all agents into one file
        """
        self.prep_training(device='cpu')  # move parameters to CPU before saving
        save_dict = {'init_dict': self.init_dict,
                     'agent_params': [a.get_params() for a in self.agents],
                     'critic_params': {'critic': self.critic.state_dict(),
                                       'target_critic': self.target_critic.state_dict(),
                                       'critic_optimizer': self.critic_optimizer.state_dict()},
                     'episode': episode}
        torch.save(save_dict, filename)

    @classmethod
    def init_from_env(cls, env,
                      spatial_tensors,
                      batch_size,
                      gamma=0.95, tau=0.01,
                      pi_lr=0.01, q_lr=0.01,
                      reward_scale=10.,
                      pol_hidden_dim=64, critic_hidden_dim=64, attend_heads=4,
                      **kwargs):
        # TODO changed the hidden dim from 128 to 64
        """
        Instantiate instance of this class from multi-agent environment

        env: Multi-agent Gym environment
        gamma: discount factor
        tau: rate of update for target networks
        lr: learning rate for networks
        hidden_dim: number of hidden dimensions for networks
        """
        agent_init_params = []
        # sa_size = []
        s_size = []
        a_size = []
        for acsp, obsp in zip(env.action_space,
                              env.observation_space):
            agent_init_params.append({'num_in_pol': np.ones(shape=obsp['image'].shape).flatten().shape[0],
                                      'num_out_pol': acsp.n})
            # sa_size.append((obsp['image'].shape, acsp.n))
            # s_size.append(obsp['image'].shape)
            a_size.append(acsp.n)

        init_dict = {'gamma': gamma,
                     'tau': tau,
                     'pi_lr': pi_lr,
                     'q_lr': q_lr,
                     'reward_scale': reward_scale,
                     'pol_hidden_dim': pol_hidden_dim,
                     'critic_hidden_dim': critic_hidden_dim,
                     'agent_init_params': agent_init_params,
                     'n_agents': env.n_agents,
                     # 'sa_size': sa_size,
                     'spatial_tensors':spatial_tensors,
                     'batch_size': batch_size,
                     'n_actions': a_size,
                     # 's_size': s_size,
                     'input_dims': [env.obs_shape['unary'][-1], env.obs_shape['binary'][-1] ],
                     # 3 attributes and 14 relations

                     }
        instance = cls(**init_dict)
        instance.init_dict = init_dict
        return instance

    @classmethod
    def init_from_save(cls, filename, load_critic=False):
        """
        Instantiate instance of this class from file created by 'save' method
        """
        save_dict = torch.load(filename, map_location="cuda")
        # episode = save_dict['episode']
        episode = 29001 +8200
        instance = cls(**save_dict['init_dict'])
        instance.init_dict = save_dict['init_dict']
        for a, params in zip(instance.agents, save_dict['agent_params']):
            a.load_params(params)
        instance.pol_dev = 'gpu'
        instance.trgt_pol_dev = 'gpu'
        if load_critic:
            critic_params = save_dict['critic_params']

            instance.critic.load_state_dict(critic_params['critic'])
            instance.critic = instance.critic.to('cuda')

            instance.target_critic.load_state_dict(critic_params['target_critic'])
            instance.target_critic = instance.target_critic.to('cuda')
            instance.critic_optimizer = Adam(instance.critic.parameters(), lr=0.01, weight_decay=1e-3)
            instance.critic_optimizer.load_state_dict(critic_params['critic_optimizer'])
            instance.critic_dev = 'gpu'
            instance.trgt_critic_dev = 'gpu'


        return instance, episode


