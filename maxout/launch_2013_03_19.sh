jobdispatch --torque --env=THEANO_FLAGS=device=gpu,floatX=float32,force_device=True --duree=48:00:00 --whitespace --gpu train.py $G/maxout/expdir/david_full_norm_C"{{1,2,3,4,5,6}}".yaml
