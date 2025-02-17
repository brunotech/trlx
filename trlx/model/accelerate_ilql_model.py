import os
from typing import Dict, Iterable

from trlx.model import BaseRLModel, register_model
from trlx.pipeline.offline_pipeline import OfflineRolloutStorage, OfflinePipeline

from trlx.model.nn.ilql_models import QVModel
from trlx.utils import rampup_decay, safe_mkdir, Clock, topk_mask

from transformers import AutoTokenizer, AutoConfig
import wandb

import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader
import numpy as np

from accelerate import Accelerator

WORLD_SIZE = int(os.environ.get('WORLD_SIZE', 1))
LOCAL_RANK = int(os.environ.get('LOCAL_RANK', 0))

@register_model
class ILQLModel(BaseRLModel):
    def __init__(self, config, gpt_config_or_path, tokenizer=None, logit_mask=None, train_mode=True):
        super().__init__(config, train_mode)

        self.model = QVModel(gpt_config_or_path, config.method)
        self.max_length = config.train.gen_size
        self.tokenizer = tokenizer
        self.logit_mask = logit_mask

        self.accelerator = Accelerator(log_with='wandb')
        self.accelerator.print(os.environ)

        if WORLD_SIZE > 1:
            torch.distributed.barrier(device_ids=[LOCAL_RANK])
        else:
            torch.random.manual_seed(1000)

        if self.accelerator.is_main_process:
            self.accelerator.init_trackers(project_name=config.train.project_name, config=config.to_dict())

        if self.train_mode:
            self.opt = torch.optim.AdamW(self.model.parameters(), lr = self.config.train.learning_rate_init)
            self.scheduler = rampup_decay(
                self.config.train.lr_ramp_steps,
                self.config.train.lr_decay_steps,
                self.config.train.learning_rate_target / self.config.train.learning_rate_init,
                self.opt
            )

    def tokenize(self, texts):
        return self.tokenizer(
            [self.tokenizer.bos_token + x + self.tokenizer.eos_token for x in texts],
            max_length=self.max_length,
            truncation=True,
            padding=True,
            return_tensors='pt'
        )

    def get_components(self) -> Dict[str, any]:
        components = {
            "model" : self.model,
            "opt" : self.opt,
            "scheduler" : self.scheduler} if self.train_mode else {"model" : self.model}
        return components

    def learn(self):
        timer = Clock()

        eos_token_id = self.tokenizer.eos_token_id if self.tokenizer else 0
        train_dataloader = self.train_store.create_loader(self.config.train.batch_size, eos_token_id=eos_token_id)
        eval_dataloader = self.eval_pipeline.create_loader(self.config.train.batch_size, shuffle=False)

        self.model, self.opt, train_dataloader, eval_dataloader = self.accelerator.prepare(
            self.model, self.opt, train_dataloader, eval_dataloader
        )

        opt_steps = 0
        for epoch in range(self.config.train.epochs):
            evals_stats = {}
            logs = {}
            for batch in train_dataloader:
                if opt_steps % self.config.train.eval_interval == 0:
                    self.model.eval()

                    all_samples = []
                    for prompts in eval_dataloader:
                        with torch.no_grad():
                            samples, _ = self.model.sample(
                                prompts,
                                beta=self.model.beta,
                                max_length=self.config.train.gen_size,
                                logit_mask=self.logit_mask
                            )

                        all_samples.append(samples)

                    samples = self.accelerator.gather(torch.vstack(all_samples))

                    if self.accelerator.is_main_process:
                        rewards = torch.tensor(self.reward_fn(samples), dtype=float)
                        reward = rewards.mean()

                        if self.stats_fn:
                            eval_stats = self.stats_fn(samples)
                            logs.update(eval_stats)

                        if self.tokenizer:
                            texts = self.tokenizer.batch_decode(samples, skip_special_tokens=True)
                            pairs = list(zip(texts, rewards))
                            logs['samples'] = wandb.Table(columns=['samples', 'reward'], rows=pairs[:16])
                            if os.environ.get('DEBUG'):
                                print(f'\n'.join([f'[{reward:.2f}] {text}' for text, reward in pairs[:10]]))
                        else:
                            if os.environ.get('DEBUG'):
                                print(samples)

                        logs['reward'] = reward

                    self.model.train()

                loss, stats = self.model.loss(batch)

                if opt_steps % self.config.train.eval_interval == 0:
                    logs.update(stats)
                    self.accelerator.log(logs)

                self.accelerator.backward(loss)
                self.opt.step()
                self.opt.zero_grad()
                self.scheduler.step()
                opt_steps += 1

                if opt_steps % self.config.method.steps_for_target_q_sync == 0:
                    self.model.sync_target_q_heads()
