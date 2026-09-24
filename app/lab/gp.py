from __future__ import annotations
import math,random

def kernel(a,b,lengths=None):
    lengths=lengths or [0.25]*len(a);r=math.sqrt(sum(((x-y)/l)**2 for x,y,l in zip(a,b,lengths)));z=math.sqrt(5.0)*r
    return (1+z+5*r*r/3)*math.exp(-z)
def cholesky(a):
    n=len(a);l=[[0.0]*n for _ in range(n)]
    for i in range(n):
      for j in range(i+1):
        s=sum(l[i][k]*l[j][k] for k in range(j));l[i][j]=math.sqrt(max(a[i][i]-s,1e-12)) if i==j else (a[i][j]-s)/l[j][j]
    return l
def solve_cholesky(l,b):
    n=len(b);y=[0.0]*n
    for i in range(n):y[i]=(b[i]-sum(l[i][j]*y[j] for j in range(i)))/l[i][i]
    x=[0.0]*n
    for i in range(n-1,-1,-1):x[i]=(y[i]-sum(l[j][i]*x[j] for j in range(i+1,n)))/l[i][i]
    return x
def fit(xs,ys,noise=1e-6):
    mean=sum(ys)/len(ys);sd=math.sqrt(sum((v-mean)**2 for v in ys)/max(1,len(ys)-1)) or 1.0;z=[(v-mean)/sd for v in ys]
    matrix=[[kernel(a,b)+(noise if i==j else 0.0) for j,b in enumerate(xs)] for i,a in enumerate(xs)];l=cholesky(matrix)
    return mean,sd,l,solve_cholesky(l,z)
def predict_fitted(xs,x,model):
    mean,sd,l,alpha=model;kx=[kernel(a,x) for a in xs];mu=sum(a*b for a,b in zip(kx,alpha))*sd+mean;v=solve_cholesky(l,kx);var=max(1e-12,kernel(x,x)-sum(a*b for a,b in zip(kx,v)))*sd*sd
    return mu,math.sqrt(var)
def predict(xs,ys,x,noise=1e-6):return predict_fitted(xs,x,fit(xs,ys,noise))
def expected_improvement(mu,sigma,best,xi=0.01):
    if sigma<=1e-12:return 0.0
    imp=mu-best-xi;z=imp/sigma;phi=math.exp(-.5*z*z)/math.sqrt(2*math.pi);cdf=.5*(1+math.erf(z/math.sqrt(2)));return imp*cdf+sigma*phi
def propose(xs,ys,seed,pool=2048,min_distance=1e-6):
    rng=random.Random(seed);best_x=None;best_ei=-1.0;incumbent=max(ys);model=fit(xs,ys)
    for _ in range(pool):
      x=[rng.random() for _ in range(len(xs[0]))]
      if any(sum((a-b)**2 for a,b in zip(x,old))<min_distance for old in xs):continue
      mu,sigma=predict_fitted(xs,x,model);ei=expected_improvement(mu,sigma,incumbent)
      if ei>best_ei:best_x,best_ei=x,ei
    if best_x is None:raise RuntimeError('no non-duplicate GP candidate')
    return best_x,best_ei
