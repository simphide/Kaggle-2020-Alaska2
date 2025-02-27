export KAGGLE_2020_ALASKA2=/home/bloodaxe/datasets/ALASKA2

#python -m torch.distributed.launch --nproc_per_node=4 train_d.py -m hpf_net -b 26 -w 8 -d 0.2 -s cos -o SGD --epochs 75 -a medium\
#  --modification-flag-loss bce 1 --modification-type-loss ce 1 -lr 1e-2 -wd 1e-4 --fold 2 --seed 10002 -v --fp16

#python -m torch.distributed.launch --nproc_per_node=4 train_d.py -m hpf_b3_fixed_covpool -b 22 -w 4 -d 0.2 -s cos -o SGD --epochs 75 -a medium\
#  --modification-flag-loss bce 1 --modification-type-loss ce 1 -lr 1e-2 -wd 1e-4 --fold 2 --seed 10002 -v --fp16

python -m torch.distributed.launch --nproc_per_node=4 train_d.py -m hpf_b3_covpool -b 20 -w 4 -d 0.2 -s cos -o SGD --epochs 75 -a medium\
  --modification-flag-loss bce 1 --modification-type-loss ce 1 -lr 1e-3 -wd 1e-4 --fold 2 --seed 100002 -v --fp16\
  --transfer /home/bloodaxe/develop/Kaggle-2020-Alaska2/runs/Jun30_23_10_hpf_b3_fixed_covpool_fold2_local_rank_0_fp16/main/checkpoints_auc_classifier/last.pth

#python -m torch.distributed.launch --nproc_per_node=4 train_d.py -m hpf_b3_fixed_gap -b 22 -w 4 -d 0.2 -s cos -o SGD --epochs 75 -a medium\
#  --modification-flag-loss bce 1 --modification-type-loss ce 1 -lr 1e-2 -wd 1e-4 --fold 2 --seed 10002 -v --fp16
