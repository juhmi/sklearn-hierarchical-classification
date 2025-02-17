"""
Hierarchical classifier interface.

"""
import numpy as np
from networkx import DiGraph, is_tree
from scipy.sparse import csr_matrix
from sklearn.base import (
    BaseEstimator,
    ClassifierMixin,
    MetaEstimatorMixin,
    clone,
)
from sklearn.dummy import DummyClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.utils.multiclass import check_classification_targets
from sklearn.utils.validation import (
    check_array,
    check_consistent_length,
    check_is_fitted,
    check_X_y,
)

from sklearn_hierarchical_classification.array import (
    apply_along_rows,
    apply_rollup_Xy,
    apply_rollup_Xy_raw,
    extract_rows_csr,
    flatten_list,
    nnz_rows_ix,
)
from sklearn_hierarchical_classification.constants import (
    CLASSIFIER,
    DEFAULT,
    METAFEATURES,
    ROOT,
)
from sklearn_hierarchical_classification.decorators import logger
from sklearn_hierarchical_classification.dummy import DummyProgress
from sklearn_hierarchical_classification.graph import make_flat_hierarchy, rollup_nodes
from sklearn_hierarchical_classification.validation import is_estimator, validate_parameters


@logger
class HierarchicalClassifier(BaseEstimator, ClassifierMixin, MetaEstimatorMixin):
    """Hierarchical classification strategy

    Hierarchical classification deals with the scenario where our target classes have
    inherent structure that can be represented as a tree or a directed acyclic graph (DAG),
    with nodes representing the target classes themselves, and edges representing their inter-relatedness,
    e.g "IS A" semantics.

    Within this general framework, several distinctions can be made based on a few key modelling decisions:

    - Multi-label classification - Do we support classifying into more than a single target class/label
    - Mandatory / Non-mandatory leaf node prediction - Do we require that classification always results with
        classes corresponding to leaf nodes, or can intermediate nodes also be treated as valid output predictions.
    - Local classifiers - the local (or "base") classifiers can theoretically be chosen to be of any kind, but we
        distinguish between three main modes of local classification:
            * "One classifier per parent node" - where each non-leaf node can be fitted with a multi-class
                classifier to predict which one of its child nodes is relevant for given example.
            * "One classifier per node" - where each node is fitted with a binary "membership" classifier which
                returns a binary (or a probability) score indicating the fitness for that node and the current
                example.
            * Global / "big bang" classifiers - where a single classifier predicts the full path in the hierarchy
                for a given example.

    The nomenclature used here is based on the framework outlined in [1].

    Parameters
    ----------
    base_estimator : classifier object, function, dict, or None
        A scikit-learn compatible classifier object implementing "fit" and "predict_proba" to be used as the
        base classifier.
        If a callable function is given, it will be called to evaluate which classifier to instantiate for
        current node. The function will be called with the current node and the graph instance.
        Alternatively, a dictionary mapping classes to classifier objects can be given. In this case,
        when building the classifier tree, the dictionary will be consulted and if a key is found matching
        a particular node, the base classifier pointed to in the dict will be used. Since this is most often
        useful for specifying classifiers on only a handlful of objects, a special "DEFAULT" key can be used to
        set the base classifier to use as a "catch all".
        If not provided, a base estimator will be chosen by the framework using various meta-learning
        heuristics (WIP).

    class_hierarchy : networkx.DiGraph object, or dict-of-dicts adjacency representation (see examples)
        A directed graph which represents the target classes and their relations. Must be a tree/DAG (no cycles).
        If not provided, this will be initialized during the `fit` operation into a trivial graph structure linking
        all classes given in `y` to an artificial "ROOT" node.

    prediction_depth : "mlnp", "nmlnp"
        Prediction depth requirements. This corresponds to whether we wish the classifier to always terminate at
        a leaf node (mandatory leaf-node prediction, "mlnp"), or wish to support early termination via some
        stopping criteria (non-mandatory leaf-node prediction, "nmlnp"). When "nmlnp" is specified, the
        stopping_criteria parameter is used to control the behaviour of the classifier.

    algorithm : "lcn", "lcpn"
        The algorithm type to use for building the hierarchical classification, according to the
        taxonomy defined in [1].

        "lcpn" (which is the default) stands for "local classifier per parent node". Under this model,
        a multi-class classifier is trained at each parent node, to distinguish between each child nodes.

        "lcn", which stands for "local classifier per node". Under this model, a binary classifier is trained
        at each node. Under this model, a further distinction is made based on how the training data set is constructed.
        This is controlled by the "training_strategy" parameter.

    training_strategy: "exclusive", "less_exclusive", "inclusive", "less_inclusive",
                       "siblings", "exclusive_siblings", or None.
        This parameter is used when the "algorithm" parameter is to set to "lcn", and dictates how training data
        is constructed for training the binary classifier at each node.

    stopping_criteria: function, float, or None.
        This parameter is used when the "prediction_depth" parameter is set to "nmlnp", and is used to evaluate
        at a given node whether classification should terminate or continue further down the hierarchy.

        When set to a float, the prediction will stop if the reported confidence at current classifier is below
        the provided value.

        When set to a function, the callback function will be called with the current node attributes,
        including its metafeatures, and the current classification results.
        This allows the user to define arbitrary logic that can decide whether classification should stop at
        the current node or continue. The function should return True if classification should continue,
        or False if classification should stop at current node.

    root : integer, string
        The unique identifier for the qualified root node in the class hierarchy. The hierarchical classifier
        assumes that the given class hierarchy graph is a rooted DAG, e.g has a single designated root node
        of in-degree 0. This node is associated with a special identifier which defaults to a framework provided one,
        but can be overridden by user in some cases, e.g if the original taxonomy is already rooted and there"s no need
        for injecting an artifical root node.

    progress_wrapper : progress generator or None
        If value is set, will attempt to use the given generator to display progress updates. This added functionality
        is especially useful within interactive environments (e.g in a testing harness or a Jupyter notebook). Setting
        this value will also enable verbose logging. Common values in tqdm are `tqdm_notebook` or `tqdm`

    feature_extraction : "preprocessed", "raw"
        Determines the feature extraction policy the classifier uses.
        When set to "raw", the classifier will expect the raw training examples are passed in to `.fit()` and `.train()`
        as X. This means that the base_estimator should point to a sklearn Pipeline that includes feature extraction.
        When set to "preprocessed", the classifier will expect X to be a pre-computed feature (sparse) matrix.

    mlb : MultiLabelBinarizer or None
        For multi-label classification, the MultiLabelBinarizer instance that was used for creating the y variable.

    mlb_prediction_threshold : float
        For multi-label prediction tasks (when `mlb` is set to a MultiLabelBinarizer instance), can define a prediction
        score threshold to use for considering a label to be a prediction. Defaults to zero.

    use_decision_function : bool
        Some classifiers (e.g. sklearn.svm.SVC) expose a `.decision_function()` method which would take in the
        feature matrix X and return a set of per-sample scores, corresponding to each label. Setting this to True
        would attempt to use this method when it is exposed by the base classifier.

    Attributes
    ----------
    classes_ : array, shape = [`n_classes`]
        Flat array of class labels

    References
    ----------

    .. [1] CN Silla et al., "A survey of hierarchical classification across
           different application domains", 2011.

    """

    def __init__(
        self,
        base_estimator=None,
        class_hierarchy=None,
        prediction_depth="mlnp",
        algorithm="lcpn",
        training_strategy=None,
        stopping_criteria=None,
        root=ROOT,
        progress_wrapper=None,
        feature_extraction="preprocessed",
        mlb=None,
        mlb_prediction_threshold=0.,
        use_decision_function=False,
    ):
        self.estimators_ = {}
        self.base_estimator = base_estimator
        self.class_hierarchy = class_hierarchy
        self.prediction_depth = prediction_depth
        self.algorithm = algorithm
        self.training_strategy = training_strategy
        self.stopping_criteria = stopping_criteria
        self.root = root
        self.progress_wrapper = progress_wrapper
        self.feature_extraction = feature_extraction
        self.mlb = mlb
        self.mlb_prediction_threshold = mlb_prediction_threshold
        self.use_decision_function = use_decision_function

    def fit(self, X, y=None, sample_weight=None):
        """Fit underlying classifiers.

        Parameters
        ----------
        X : (sparse) array-like, shape = [n_samples, n_features]
            Data.

        y : (sparse) array-like, shape = [n_samples, ], [n_samples, n_classes]
            Multi-class targets. An indicator matrix turns on multilabel
            classification.

        sample_weight : array-like, shape (n_samples,), optional (default=None)
            Weights applied to individual samples (1. for unweighted).

        Returns
        -------
        self

        """
        if self.feature_extraction == "raw":
            # In raw mode, only validate targets (y) format and
            # that targets and training data (X) are of same cardinality, since
            # X will in general not be a 2D feature matrix, but rather the raw training examples,
            # e.g. text snippets or images.
            y = check_array(
                y,
                accept_sparse="csr",
                force_all_finite=True,
                ensure_2d=False,
                dtype=None,
            )
            if len(X) != y.shape[0]:
                raise ValueError("bad input shape: len(X) != y.shape[0]")
        else:
            X, y = check_X_y(X, y, accept_sparse="csr")

        check_classification_targets(y)
        if sample_weight is not None:
            check_consistent_length(y, sample_weight)

        # Check that parameter assignment is consistent
        self._check_parameters()

        # Initialize NetworkX Graph from input class hierarchy
        self.class_hierarchy_ = self.class_hierarchy or make_flat_hierarchy(list(np.unique(y)), root=self.root)
        self.graph_ = DiGraph(self.class_hierarchy_)
        self.is_tree_ = is_tree(self.graph_)
        self.classes_ = list(
            node
            for node in self.graph_.nodes()
            if node != self.root
        )

        if self.feature_extraction == "preprocessed":
            # When not in raw mode, recursively build training feature sets for each node in graph
            with self._progress(total=self.n_classes_ + 1, desc="Building features") as progress:
                self._recursive_build_features(X, y, node_id=self.root, progress=progress)

        # Recursively train base classifiers
        with self._progress(total=self.n_classes_ + 1, desc="Training base classifiers") as progress:
            self._recursive_train_local_classifiers(X, y, node_id=self.root, progress=progress)

        return self

    def predict(self, X):
        """Predict multi-class targets using underlying estimators.

        Parameters
        ----------
        X : (sparse) array-like, shape = [n_samples, n_features]
            Data.

        Returns
        -------
        y : (sparse) array-like, shape = [n_samples, ], [n_samples, n_classes].
            Predicted multi-class targets.

        """
        check_is_fitted(self, "graph_")

        def _classify(x):
            path, _ = self._recursive_predict(x, root=self.root)
            if self.mlb:
                return path
            else:
                return path[-1]

        if self.feature_extraction == "raw":
            return np.array([
                _classify(X[i])
                for i in range(len(X))
            ])
        else:
            X = check_array(X, accept_sparse="csr")

        y_pred = apply_along_rows(_classify, X=X)
        return y_pred

    def predict_proba(self, X):
        """
        Return probability estimates for the test vector X.

        Parameters
        ----------
        X : array-like, shape = [n_samples, n_features]

        Returns
        -------
        C : array-like, shape = [n_samples, n_classes]
            Returns the probability of the samples for each class in
            the model. The columns correspond to the classes in sorted
            order, as they appear in the attribute `classes_`.
        """
        check_is_fitted(self, "graph_")

        def _classify(x):
            _, scores = self._recursive_predict(x, root=self.root)
            return scores

        if self.feature_extraction == "raw":
            return np.array([
                _classify(X[i])
                for i in range(len(X))
            ])
        else:
            X = check_array(X, accept_sparse="csr")

        y_pred = apply_along_rows(_classify, X=X)
        return y_pred

    @property
    def n_classes_(self):
        return len(self.classes_)

    def _check_parameters(self):
        """Check the parameter assignment is valid and internally consistent."""
        validate_parameters(self)

    def _recursive_build_features(self, X, y, node_id, progress):
        """
        Build the training feature matrix X recursively, for each node.

        By default we use "hierarchical feature set" (terminology per Ceci and Malerba 2007)
        which builds up features at each node in the hiearchy by "rolling up" training examples
        defined on the the leaf nodes (classes) of the hierarchy into the parent classes relevant
        for classification at a particular non-leaf node.

        """
        if "X" in self.graph_.nodes[node_id]:
            # Already visited this node in feature building phase
            return self.graph_.nodes[node_id]["X"]

        self.logger.debug("Building features for node: %s", node_id)
        progress.update(1)

        if self.graph_.out_degree(node_id) == 0:
            # Leaf node
            indices = np.flatnonzero(y == node_id)
            self.graph_.nodes[node_id]["X"] = self._build_features(
                X=X,
                y=y,
                indices=indices,
            )

            return self.graph_.nodes[node_id]["X"]

        # Non-leaf node
        if self.feature_extraction == "raw":
            self.graph_.nodes[node_id]["X"] = []
        else:
            self.graph_.nodes[node_id]["X"] = csr_matrix(
                X.shape,
                dtype=X.dtype,
            )

        for child_node_id in self.graph_.successors(node_id):
            self.graph_.nodes[node_id]["X"] += \
                self._recursive_build_features(
                    X=X,
                    y=y,
                    node_id=child_node_id,
                    progress=progress,
                )

        # Build and store metafeatures for node
        self.graph_.nodes[node_id][METAFEATURES] = self._build_metafeatures(
            X=self.graph_.nodes[node_id]["X"],
            y=y,
        )

        # Append training data tagged with current (intermediate) node if any, and propagate up
        if not np.issubdtype(type(node_id), y.dtype):
            # If current intermediate node id type is different than that of targets array, dont bother.
            # Nb. doing this check explicitly to avoid FutureWarning, see:
            # https://stackoverflow.com/questions/40659212/futurewarning-elementwise-comparison-failed-returning-scalar-but-in-the-futur
            return self.graph_.nodes[node_id]["X"]

        indices = np.flatnonzero(y == node_id)
        X_out = self.graph_.nodes[node_id]["X"] + self._build_features(
            X=X,
            y=y,
            indices=indices,
        )
        return X_out

    def _build_features(self, X, y, indices):
        if self.feature_extraction == "raw":
            X_ = [X[ix] for ix in indices]
        else:
            X_ = extract_rows_csr(X, indices)

        # Perform feature selection
        X_ = self._select_features(X=X_, y=np.array(y)[indices])

        return X_

    def _select_features(self, X, y):
        """
        Perform feature selection for training data.

        Can be overridden by a sub-class to implement feature selection logic.

        """
        return X

    def _build_metafeatures(self, X, y):
        """
        Build the meta-features associated with a particular node.

        These are various features that can be used in training and prediction time,
        e.g the number of training samples available for the classifier trained at that node,
        the number of targets (classes) to be predicted at that node, etc.

        Parameters
        ----------
        X : (sparse) array-like, shape = [n_samples, n_features]
            The training data matrix at current node.

        Returns
        -------
        metafeatures : dict
            Python dictionary of meta-features. The following meta-features are computed by default:
            * "n_samples" - Number of samples used to train classifier at given node.
            * "n_targets" - Number of targets (classes) to classify into at given node.

        """
        if self.feature_extraction == "raw":
            # In raw mode, we do not know which training examples are "zeroed out" for which node
            # since we do not recursively build features until the recursive training phase which comes afterwards.
            # Therefore, the number of targets is simply the number of unique labels in y
            return dict(
                n_samples=len(X),
                n_targets=len(np.unique(y)),
            )

        # Indices of non-zero rows in X, i.e rows corresponding to relevant samples for this node.
        ix = nnz_rows_ix(X)

        return dict(
            n_samples=len(ix),
            n_targets=len(np.unique(y[ix])),
        )

    def _recursive_train_local_classifiers(self, X, y, node_id, progress):
        if CLASSIFIER in self.graph_.nodes[node_id]:
            # Already trained classifier at this node, skip
            return

        progress.update(1)
        self._train_local_classifier(X, y, node_id)

        for child_node_id in self.graph_.successors(node_id):
            self._recursive_train_local_classifiers(
                X=X,
                y=y,
                node_id=child_node_id,
                progress=progress,
            )

    def _train_local_classifier(self, X, y, node_id):
        if self.graph_.out_degree(node_id) == 0:
            # Leaf node
            if self.algorithm == "lcpn":
                # Leaf nodes do not get a classifier assigned in LCPN algorithm mode.
                self.logger.debug(
                    "_train_local_classifier() - skipping leaf node %s when algorithm is 'lcpn'",
                    node_id,
                )
                return

        if self.feature_extraction == "raw":
            X_ = X
            nnz_rows = range(len(X))
            Xl = len(X_)
        else:
            X = self.graph_.nodes[node_id]["X"]
            nnz_rows = nnz_rows_ix(X)
            X_ = X[nnz_rows, :]
            Xl = X_.shape

        y_rolled_up = rollup_nodes(
            graph=self.graph_,
            source=node_id,
            targets=[y[idx] for idx in nnz_rows],
            mlb=self.mlb
        )

        if self.is_tree_:
            if self.mlb is None:
                y_ = flatten_list(y_rolled_up)
            else:
                y_ = self.mlb.transform(y_rolled_up)
                # take all non zero, only compare in side the siblings
                idx = np.where(y_.sum(1) > 0)[0]
                y_ = y_[idx, :]
                if self.feature_extraction == "raw":
                    X_ = [X_[tk] for tk in idx]
                else:
                    X_ = X_[idx, :]
        else:
            # Class hierarchy graph is a DAG
            if self.feature_extraction == "raw":
                X_, y_ = apply_rollup_Xy_raw(X_, y_rolled_up)
            else:
                X_, y_ = apply_rollup_Xy(X_, y_rolled_up)

        num_targets = len(np.unique(y_))

        self.logger.debug(
            "_train_local_classifier() - Training local classifier for node: %s, X_.shape: %s, len(y): %s, n_targets: %s",  # noqa:E501
            node_id,
            Xl,
            len(y_),
            num_targets,
        )

        if self.feature_extraction == "preprocessed" and X_.shape[0] == 0:
            # No training data could be materialized for current node
            # TODO: support a "strict" mode flag to explicitly enable/disable fallback logic here?
            self.logger.warning(
                "_train_local_classifier() - not enough training data available to train, classification in branch will terminate at node %s",  # noqa:E501
                node_id,
            )
            return
        elif num_targets == 1:
            # Training data could be materialized for only a single target at current node
            # TODO: support a "strict" mode flag to explicitly enable/disable fallback logic here?
            constant = y_[0]
            self.logger.debug(
                "_train_local_classifier() - only a single target (child node) available to train classifier for node %s, Will trivially predict %s",  # noqa:E501
                node_id,
                constant,
            )

            clf = DummyClassifier(strategy="constant", constant=constant)
        else:
            clf = self._base_estimator_for(node_id)

        if self.feature_extraction == "raw":
            if len(X_) > 0:
                clf.fit(X=X_, y=y_)
                self.logger.debug(
                    "_train_local_classifier() - training node %s ",  # noqa:E501
                    node_id,
                )
                self.graph_.nodes[node_id][CLASSIFIER] = clf
            else:
                self.logger.debug(
                    "_train_local_classifier() - could not train  node %s ",  # noqa:E501
                    node_id,
                )

        else:
            clf.fit(X=X_, y=y_)
            self.graph_.nodes[node_id][CLASSIFIER] = clf
        self.estimators_[node_id] = clf

    def _recursive_predict(self, x, root):  # noqa:C901 TODO: refactor
        if CLASSIFIER not in self.graph_.nodes[root]:
            return None, None

        clf = self.graph_.nodes[root][CLASSIFIER]
        path = [root]
        path_proba = []
        class_proba = np.zeros_like(self.classes_, dtype=np.float64)

        while clf:
            if self.use_decision_function and hasattr(clf, "decision_function"):
                if self.feature_extraction == "raw":
                    probs = clf.decision_function([x])
                    argmax = np.argmax(probs)
                    score = probs[0, argmax]

                else:
                    probs = clf.decision_function(x)
                    argmax = np.argmax(probs)
                    score = probs[argmax]
            else:
                probs = clf.predict_proba(x)[0]
                argmax = np.argmax(probs)
                score = probs[argmax]

            path_proba.append(score)
            if self.mlb is not None:
                predictions = []

            # Report probabilities in terms of complete class hierarchy
            if len(clf.classes_) == 1:
                prediction = clf.classes_[0]

            for local_class_idx, class_ in enumerate(clf.classes_):
                if self.mlb:
                    # when we have a multi-label binarizer
                    class_idx = class_
                    class_proba[class_idx] = probs[0, local_class_idx]
                    if class_proba[class_idx] > self.mlb_prediction_threshold:
                        predictions.append(self.mlb.classes_[class_])
                else:
                    try:
                        class_idx = self.classes_.index(class_)
                    except ValueError:
                        # This may happen if the classes_ enumeration we construct during fit()
                        # has a mismatch with the individual node classifiers" classes_.
                        self.logger.error(
                            "Could not find index in self.classes_ for class_ = '%s' (type: %s). path: %s",
                            class_,
                            type(class_),
                            path,
                        )
                        raise
                    if len(probs.shape) > 1 and probs.shape[0] == 1:
                        class_proba[class_idx] = probs[0, local_class_idx]
                        if local_class_idx == argmax:
                            prediction = class_
                    else:
                        class_proba[class_idx] = probs[local_class_idx]
                        if local_class_idx == argmax:
                            prediction = class_

            if self.mlb is None:
                if self._should_early_terminate(
                    current_node=path[-1],
                    prediction=prediction,
                    score=score,
                ):
                    break

                # Update current path
                path.append(prediction)
                clf = self.graph_.nodes[prediction].get(CLASSIFIER, None)
            else:
                clf = None
                for prediction in predictions:
                    pred_path, preds_prob = self._recursive_predict(x, prediction)
                    path.append(prediction)
                    if preds_prob is not None:
                        class_proba += preds_prob
                        path.extend(pred_path)

        return path, class_proba

    def _should_early_terminate(self, current_node, prediction, score):
        """
        Evaluate whether classification should terminate at given step.

        This depends on whether early-termination, as dictated by the the "prediction_depth"
          and "stopping_criteria" parameters, is triggered.

        """
        if self.prediction_depth != "nmlnp":
            # Prediction depth parameter does not allow for early termination
            return False

        if (
            isinstance(self.stopping_criteria, float)
            and score < self.stopping_criteria
        ):
            if current_node == self.root:
                return False

            self.logger.debug(
                "_should_early_terminate() - score %s < %s, terminating at node %s",
                score,
                self.stopping_criteria,
                current_node,
            )
            return True
        elif callable(self.stopping_criteria):
            return self.stopping_criteria(
                current_node=self.graph_.nodes[current_node],
                prediction=prediction,
                score=score,
            )

        return False

    def _base_estimator_for(self, node_id):
        base_estimator = None
        if self.base_estimator is None:
            # No base estimator specified by user, try to pick best one
            base_estimator = self._make_base_estimator(node_id)

        elif isinstance(self.base_estimator, dict):
            # User provided dictionary mapping nodes to estimators
            if node_id in self.base_estimator:
                base_estimator = self.base_estimator[node_id]
            else:
                base_estimator = self.base_estimator[DEFAULT]

        elif is_estimator(self.base_estimator):
            # Single base estimator object, return a copy
            base_estimator = self.base_estimator

        else:
            # By default, treat as callable factory
            base_estimator = self.base_estimator(node_id=node_id, graph=self.graph_)

        return clone(base_estimator)

    def _make_base_estimator(self, node_id):
        """Create a default base estimator if a more specific one was not chosen by user."""
        return LogisticRegression(
            solver="lbfgs",
            max_iter=1000,
            multi_class="multinomial",
        )

    def _progress(self, total, desc, **kwargs):
        if self.progress_wrapper:
            return self.progress_wrapper(total=total, desc=desc)
        else:
            return DummyProgress()
