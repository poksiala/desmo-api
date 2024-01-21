from statemachine.contrib.diagram import DotGraphMachine
from desmo_api.fsm import JailStateMachine


if __name__ == "__main__":
    graph = DotGraphMachine(JailStateMachine)
    graph.graph_rankdir = "TB"
    dot = graph()
    dot.write_png("fsm-desmo.png")
