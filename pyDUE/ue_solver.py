'''
Created on Apr 20, 2014

@author: jeromethai
'''

import numpy as np
from cvxopt import matrix, spmatrix, solvers, spdiag, mul, div, sparse
import rank_nullspace as rn
from util import find_basis
from kktsolver import get_kktsolver
import networkx as nx
import itertools
import scipy.optimize as op
from util import create_networkx_graph
import logging
if logging.getLogger().getEffectiveLevel() >= logging.DEBUG:
    solvers.options['show_progress'] = False
else:
    solvers.options['show_progress'] = True



def constraints(graph, rm_redundant = False):
    """Construct constraints for the UE link flow
    
    Parameters
    ----------
    graph: graph object
    rm_redundant: if True, remove redundant constraints for the ue solver
    
    Return value
    ------------
    Aeq, beq: equality constraints Aeq*x = beq
    """
    C, ind = nodelink_incidence(graph, rm_redundant)
    ds = [get_demands(graph, ind, id) for id,node in graph.nodes.items() if len(node.endODs) > 0]
    p = len(ds)
    m,n = C.size
    Aeq, beq = spmatrix([], [], [], (p*m,p*n)), matrix(ds)
    for k in range(p): Aeq[k*m:(k+1)*m, k*n:(k+1)*n] = C
    return Aeq, beq


def nodelink_incidence(graph, rm_redundant = False):
    """
    get node-link incidence matrix
    
    Parameters
    ----------
    graph: graph object
    rm_redundant: if True, remove redundant constraints for the ue solver
    
    Return value
    ------------
    C: matrix of incidence node-link
    ind: indices of a basis formed by the rows of C
    """
    m, n = graph.numnodes, graph.numlinks
    entries, I, J = [], [], []
    for id1,node in graph.nodes.items():
        for id2,link in node.inlinks.items(): entries.append(1.0); I.append(id1-1); J.append(graph.indlinks[id2])
        for id2,link in node.outlinks.items(): entries.append(-1.0); I.append(id1-1); J.append(graph.indlinks[id2])
    C = spmatrix(entries, I, J, (m,n))
    if rm_redundant:
        M = matrix(C)
        r = rn.rank(M)
        if r < m:
            print 'Remove {} redundant constraint(s)'.format(m-r)
            ind = find_basis(M.trans())
            return C[ind,:], ind
    return C, range(m)


def get_demands(graph, ind, node_id):
    """
    get demands for all OD pairs sharing the same destination
    
    Parameters
    ----------
    graph: graph object
    ind: indices of a basis formed by the rows of C
    node_id: id of the destination node
    """
    d = matrix(0.0, (graph.numnodes,1))
    for OD in graph.nodes[node_id].endODs.values():
        d[node_id-1] += OD.flow
        d[OD.o-1] = -OD.flow
    return d[ind]


def objective_poly(x, z, ks, p, w_obs=0.0, obs=None, l_obs=None, w_gap=1.0):
    """Objective function of UE program with polynomial delay functions
    f(x) = sum_i f_i(l_i) (+ 0.5*w_obs*||l[obs]-l_obs||^2)
    f_i(u) = sum_{k=1}^degree ks[i,k] u^k
    with l = sum_w x_w
    
    Parameters
    ----------
    x,z: variables for the F(x,z) function for cvxopt.solvers.cp
    ks: matrix of size (n,degree) 
    p: number of w's
    w_obs: weight on the observation residual
    obs: indices of the observed links
    l_obs: observations
    """
    n, d = ks.size
    if x is None: return 0, matrix(1.0/p, (p*n,1))
    l = matrix(0.0, (n,1))
    for k in range(p): l += x[k*n:(k+1)*n]
    f, Df, H = 0.0, matrix(0.0, (1,n)), matrix(0.0, (n,1))
    for i in range(n):
        tmp = matrix(np.power(l[i],range(d+1)))
        f += ks[i,:] * tmp[1:]
        Df[i] = ks[i,:] * mul(tmp[:-1], matrix(range(1,d+1)))
        H[i] = ks[i,1:] * mul(tmp[:-2], matrix(range(2,d+1)), matrix(range(1,d)))
    if w_gap != 1.0: f, Df, H = w_gap*f, w_gap*Df, w_gap*H
    
    if w_obs > 0.0:
        num_obs, e = len(obs), l[obs]-l_obs
        f += 0.5*w_obs*e.T*e
        Df += w_obs*spmatrix(e, [0]*num_obs, obs, (1,n))
        H += spmatrix([w_obs]*num_obs, obs, [0]*num_obs, (n,1))
    
    Df = matrix([[Df]]*p)
    if z is None: return f, Df
    return f, Df, matrix([[spdiag(z[0] * H)]*p]*p)


def objective_hyper(x, z, ks, p):
    """Objective function of UE program with hyperbolic delay functions
    f(x) = sum_i f_i(v_i) with v = sum_w x_w
    f_i(u) = ks[i,0]*u - ks[i,1]*log(ks[i,2]-u)
    
    Parameters
    ----------
    x,z: variables for the F(x,z) function for cvxopt.solvers.cp
    ks: matrix of size (n,3) 
    p: number of destinations
    (we use multiple-sources single-sink node-arc formulation)
    """
    n = ks.size[0]
    if x is None: return 0, matrix(1.0/p, (p*n,1))
    l = matrix(0.0, (n,1))
    for k in range(p): l += x[k*n:(k+1)*n]
    f, Df, H = 0.0, matrix(0.0, (1,n)), matrix(0.0, (n,1))
    for i in range(n):
        tmp = 1.0/(ks[i,2]-l[i])
        f += ks[i,0]*l[i] - ks[i,1]*np.log(max(ks[i,2]-l[i], 1e-13))
        Df[i] = ks[i,0] + ks[i,1]*tmp
        H[i] = ks[i,1]*tmp**2
    Df = matrix([[Df]]*p)
    if z is None: return f, Df
    return f, Df, sparse([[spdiag(z[0] * H)]*p]*p)


def objective_hyper_SO(x, z, ks, p):
    """Objective function of SO program with hyperbolic delay functions
    f(x) = \sum_i f_i(v_i) with v = sum_w x_w
    f_i(u) = ks[i,0]*u + ks[i,1]*u/(ks[i,2]-u)
    
    Parameters
    ----------
    x,z: variables for the F(x,z) function for cvxopt.solvers.cp
    ks: matrix of size (n,3) where ks[i,j] is the j-th parameter of the delay on link i
    p: number of destinations
    (we use multiple-sources single-sink node-arc formulation)
    """
    n = ks.size[0]
    if x is None: return 0, matrix(1.0/p, (p*n,1))
    l = matrix(0.0, (n,1))
    for k in range(p): l += x[k*n:(k+1)*n]
    f, Df, H = 0.0, matrix(0.0, (1,n)), matrix(0.0, (n,1))
    for i in range(n):
        tmp = 1.0/(ks[i,2]-l[i])
        f += ks[i,0]*l[i] + ks[i,1]*l[i]*tmp
        Df[i] = ks[i,0] + ks[i,1]*tmp + ks[i,1]*l[i]*tmp**2
        H[i] = 2.0*ks[i,1]*tmp**2 + 2.0*ks[i,1]*l[i]*tmp**3
    Df = matrix([[Df]]*p)
    if z is None: return f, Df
    return f, Df, matrix([[spdiag(z[0] * H)]*p]*p)


def get_data(graph):
    """Get data for the ue solver"""
    ## TODO deprecated
    Aeq, beq = constraints(graph)
    ffdelays = graph.get_ffdelays()
    parameters, type = graph.get_parameters()
    return Aeq, beq, ffdelays, parameters, type


def solver(graph=None, update=False, full=False, data=None, SO=False):
    """Find the UE link flow
    
    Parameters
    ----------
    graph: graph object
    update: if update==True: update link flows and link,path delays in graph
    full: if full=True, also return x (link flows per OD pair)
    data: (Aeq, beq, ffdelays, parameters, type) from get_data(graph)
    """
    if data is None: data = get_data(graph)
    Aeq, beq, ffdelays, pm, type = data
    n = len(ffdelays)
    p = Aeq.size[1]/n
    A, b = spmatrix(-1.0, range(p*n), range(p*n)), matrix(0.0, (p*n,1))
    if type == 'Polynomial':
        if not SO: pm = pm * spdiag([1.0/(j+2) for j in range(pm.size[1])])
        def F(x=None, z=None): return objective_poly(x, z, matrix([[ffdelays], [pm]]), p)
    if type == 'Hyperbolic':
        if SO:
            def F(x=None, z=None): return objective_hyper_SO(x, z, matrix([[ffdelays-div(pm[:,0],pm[:,1])], [pm]]), p)
        else:
            def F(x=None, z=None): return objective_hyper(x, z, matrix([[ffdelays-div(pm[:,0],pm[:,1])], [pm]]), p)
    dims = {'l': p*n, 'q': [], 's': []}
    x = solvers.cp(F, G=A, h=b, A=Aeq, b=beq, kktsolver=get_kktsolver(A, dims, Aeq, F))['x']
    linkflows = matrix(0.0, (n,1))
    for k in range(p): linkflows += x[k*n:(k+1)*n]
    
    if update:
        logging.info('Update link flows, delays in Graph.'); graph.update_linkflows_linkdelays(linkflows)
        logging.info('Update path delays in Graph.'); graph.update_pathdelays()
    
    if full: return linkflows, x    
    return linkflows


def solver_fw(graph=None, update=False, full=False, data=None, SO=False, e=1e-4, niter=1e4, verbose=False):
    """Frank-Wolfe algorithm for UE according to Patriksson (1994)"""
    if data is None: data = get_data(graph)
    Aeq, beq, ffdelays, pm, type = data
    nlink = len(ffdelays)
    ncol = Aeq.size[1]
    nrow = Aeq.size[0]
    npair = ncol/nlink
    nnode = nrow/npair
    G = nx.DiGraph()
    G.add_nodes_from(graph.nodes.keys())
    G.add_edges_from([(key[0],key[1]) for key in graph.links.keys()])
    for u,v in G.edges():
        link = graph.links[(u,v,1)]
        G[u][v]['obj'] = 0
        G[u][v]['cost'] = link.ffdelay
    # Step 0 (Initialization) Let f0 be a feasible solution to [TAP], LBD=0,
    # e>0, k=0 (using Dijkstra's shortest path algorithm in networkx
    LBD = 0.
    f = matrix(0.0,(ncol,1))
    iod = 0
    for OD in graph.ODs.itervalues():
        nodes_on_path = nx.dijkstra_path(G, OD.o, OD.d, weight='cost')
        for indx in xrange(len(nodes_on_path)-1):
            u = nodes_on_path[indx]
            v = nodes_on_path[indx+1]
            indx=iod*nlink+graph.indlinks[(u,v,1)]
            f[indx,0] = OD.flow
        iod += 1
        #f += [l.flow for l in graph.links.itervalues()]
    def Tf_func(f):
        linkflows = matrix(0.0, (nlink,1))
        for k in xrange(npair): linkflows += f[k*nlink:(k+1)*nlink]
        return sum([graph.links[link_key].delayfunc.compute_obj(linkflows[link_indx]) for link_key, link_indx
                in graph.indlinks.iteritems()])
    def dTf_func(f):
        linkflows = matrix(0.0, (nlink,1))
        for k in xrange(npair): linkflows += f[k*nlink:(k+1)*nlink]
        dTf = nlink*[0.0]
        for link_key, link_indx in graph.indlinks.iteritems():
            dTfi = graph.links[link_key].delayfunc.compute_delay(linkflows[link_indx]) 
            dTf[link_indx] = dTfi
        dTf = matrix(npair*dTf)
        return dTf
    for k in xrange(int(niter)):
        # Step 1 (Search direction generation) LP problem
        Tf = Tf_func(f)
        dTf = dTf_func(f)
        G = spmatrix(-1.0, range(ncol), range(ncol))
        h = matrix(ncol*[0.0])
        y = solvers.lp(dTf, G, h, Aeq, beq)['x']
        p = y - f
        # Step 2 (Convergence check)
        def T_linear(x):
            res = Tf + dTf.T*(x-f)
            return res[0,0]
        LBD = np.maximum(LBD, T_linear(y))
        if verbose:
            print 'Iter #{}a: Tf={}, T_linear={}, LBD={}, e={}'.format(k+1, Tf, T_linear(y), LBD, e)
        if (Tf - LBD) / LBD < e:
            break
        # Step 3 (Line search)
        #def T(step):
            #obj, jac = Tf_func(f+step*p), dTf_func(f+step*p).T*p
            #return [obj, np.array(jac[0])]
        #step = op.minimize(T, x0=0.5, jac=True, bounds=[(0.,1.)]).x
        def T(step):
            return  Tf_func(f+step*p)
        step = op.minimize(T, 0.5, bounds=[(0., 1.)]).x[0]
        #step = 1./(k+1)
        # Step 4 (Update)
        f += step*p
        f = matrix(f, tc='d')
        # Step 5 (Convergence check)
        if verbose:
            print 'Iter #{}b: Tf={}, step={}, LBD={}, e={}'.format(k+1, Tf_func(f), step, LBD, e)
        if (Tf_func(f) - LBD) / LBD < e: break

    linkflows = matrix(0.0, (nlink,1))
    for k in range(npair): linkflows += f[k*nlink:(k+1)*nlink]

    if update:
        logging.info('Update link flows, delays in Graph.'); graph.update_linkflows_linkdelays(linkflows)
        logging.info('Update path delays in Graph.'); graph.update_pathdelays()

    return linkflows

if __name__ == '__main__':
    from generate_graph import braess_paradox
    import time
    import datetime
    graph = braess_paradox()
    num = 1000
    starttime = time.time()
    for i in xrange(num):
        f1 = solver(graph)
    delta_time = time.time() - starttime
    print 'cvxopt: {} loops, total time {}'.format(num, datetime.timedelta(seconds=delta_time))
    starttime = time.time()
    for i in xrange(num):
        f2 = solver_fw(graph, verbose=False, e=1e-4, niter=100)
    delta_time = time.time() - starttime
    print 'fw: {} loops, total time {}'.format(num, datetime.timedelta(seconds=delta_time))
