//! Shared graph algorithms over the call graph (Epic 9).
//!
//! Kept module-local (`pub(crate)`) so Features 9.1 (entrypoints), 9.2 (spine),
//! and 9.3 (centrality) reuse one implementation rather than each copying it.

/// Iterative Tarjan strongly-connected-components — returns an SCC id per node
/// (0-based, dense). Iterative (explicit work stack) so a deep call graph can't
/// overflow the native stack. `adj` is a compact adjacency list over `0..n`.
///
/// Note: components are produced in reverse topological order, but callers that
/// need a topological order compute it explicitly rather than relying on that.
pub(crate) fn tarjan_scc(n: usize, adj: &[Vec<usize>]) -> Vec<usize> {
    const UNSET: usize = usize::MAX;
    let mut index = vec![UNSET; n];
    let mut low = vec![0usize; n];
    let mut on_stack = vec![false; n];
    let mut comp = vec![UNSET; n];
    let mut stack: Vec<usize> = Vec::new();
    let mut next_index = 0usize;
    let mut next_comp = 0usize;

    for start in 0..n {
        if index[start] != UNSET {
            continue;
        }
        let mut work: Vec<(usize, usize)> = vec![(start, 0)]; // (node, next child)
        while let Some(&(v, ci)) = work.last() {
            if ci == 0 {
                index[v] = next_index;
                low[v] = next_index;
                next_index += 1;
                stack.push(v);
                on_stack[v] = true;
            }
            if ci < adj[v].len() {
                let w = adj[v][ci];
                work.last_mut().unwrap().1 += 1;
                if index[w] == UNSET {
                    work.push((w, 0));
                } else if on_stack[w] {
                    low[v] = low[v].min(index[w]);
                }
            } else {
                if low[v] == index[v] {
                    loop {
                        let w = stack.pop().unwrap();
                        on_stack[w] = false;
                        comp[w] = next_comp;
                        if w == v {
                            break;
                        }
                    }
                    next_comp += 1;
                }
                work.pop();
                if let Some(&(parent, _)) = work.last() {
                    low[parent] = low[parent].min(low[v]);
                }
            }
        }
    }
    comp
}
