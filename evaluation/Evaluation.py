from __future__ import division
from abc import abstractmethod
from click_models.Constants import MAX_DOCS_PER_QUERY
from sklearn.metrics import mean_squared_error, roc_auc_score
import numpy as np
import math
from click_models.TCM import TCM
import collections

CLICK_TRESHOLD = 0.5
MINIMUM_OCCURENCES = 10
CUTOFF = 5

class EvaluationMethod(object):
    """
        An abstract baseclass that interfaces all evaluation methods.
    """

    def __init__(self):
        pass

    @abstractmethod
    def evaluate(self, model, sessions, **kwargs):
        """
            Classes must implement this evaluation method and return a measure.
        """
        pass


class Loglikelihood(EvaluationMethod):

    def evaluate(self, model, sessions, **kwargs):
        """
            Returns the loglikelihood of the given sessions of a particular model
        """
        
        if model.__class__ == TCM:
            len_sessions = 0
            loglikelihood = 0
            tasks = model._transform_sessions(sessions)
            for task in tasks:
                freshness = model.get_previous_examination_chance(task)
                for s_idx, session in enumerate(task):
                    log_click_probs = model.get_log_click_probs(session, freshness = freshness[s_idx])
                    loglikelihood += sum(log_click_probs) / len(log_click_probs)

            loglikelihood /= len(sessions)
        
        else:
            loglikelihood = 0
            for session in sessions:
                log_click_probs = model.get_log_click_probs(session)
                loglikelihood += sum(log_click_probs) / len(log_click_probs)

            loglikelihood /= len(sessions)
        return loglikelihood 


class Perplexity(EvaluationMethod):

    def evaluate(self, model, sessions, **kwargs):
        """
            Returns the perplexity and perplexities of a given rank for the given sessions
        """
        perplexity_at_rank = [0.0] * MAX_DOCS_PER_QUERY

        if model.__class__ == TCM:
            tasks = model._transform_sessions(sessions)
            for task in tasks:
                freshness = model.get_previous_examination_chance(task)
                for s_idx, session in enumerate(task):
                    click_probs = model.predict_click_probs(session, freshness=freshness[s_idx])
                    for rank, click_prob in enumerate(click_probs):
                        if session.web_results[rank].click:
                            p = click_prob
                        else:
                            p = 1-click_prob
                        perplexity_at_rank[rank] += math.log(p, 2)

                    
            perplexity_at_rank = [2 ** (-x / len(sessions)) for x in perplexity_at_rank]
            perplexity = sum(perplexity_at_rank) / len(perplexity_at_rank)
        
        else:
            for session in sessions:
                click_probs = model.predict_click_probs(session)
                for rank, click_prob in enumerate(click_probs):
                    if session.web_results[rank].click:
                        p = click_prob
                    else:
                        p = 1-click_prob
                    perplexity_at_rank[rank] += math.log(p, 2)

            perplexity_at_rank = [2 ** (-x / len(sessions)) for x in perplexity_at_rank]
            perplexity = sum(perplexity_at_rank) / len(perplexity_at_rank)
        return perplexity, perplexity_at_rank



class CTRPrediction(EvaluationMethod):

    def evaluate(self, model, sessions, **kwargs):
        """
            Returns the MSE of the CTR wrt the given sessions.
            Calculated according section 4.2 of:
            "A Dynamic Bayesian Network Click Model for Web Search Ranking" Chappele and Zhang, 2009
        """


        # Group sessions based on query
        session_dict = collections.defaultdict(list)
        for session in sessions:
            session_dict[session.query].append(session)
        
        MSEs, weights = [], []

        for query_id, sessions in session_dict.items():
            test_sets = []
            train_sets = []

            # For every session find out whether there is a document at the first position that also occurs in other positions
            for s_idx, session in enumerate(sessions):
                pos_1 = session.web_results[0].object
                b = False


                #Check whether session is already in a test set.
                for test in test_sets:
                    if pos_1 == test[0].web_results[0].object:
                        b = True
                if b:
                    break
                        
                
                #If not already in a test set create a new test/train pair
                test = [session]
                train = []
                
                for session_2 in sessions[:s_idx] + sessions[s_idx+1:]:

                    #Add session to test set if they have same doc in pos 1
                    if pos_1 == session_2.web_results[0].object:
                        test.append(session_2)
                    #Add session to train set if it is in another place than the first
                    elif pos_1 in [result.object for result in session_2.web_results[1:]]:
                        train.append(session_2)
                
                #Only add if there is both a test and train set.
                if test and train:
                    test_sets.append(test)
                    train_sets.append(train)


            # Train the model on the train set and get the predicted clicks of the test set.
            for test, train in zip(test_sets,train_sets):
                model.train(train)
                pred_clicks, true_clicks = 0,0
                for t in test:
                    click_prob_1 = model.predict_click_probs(t)[0]
                    if click_prob_1 > CLICK_TRESHOLD:
                        pred_clicks += 1
                    true_clicks += t.web_results[0].click

                pred_ctr = pred_clicks/len(test)
                true_ctr = true_clicks/len(test)

                # Maybe needs to be the Root Mean Square Error
                MSE = (pred_ctr - true_ctr) ** 2
                MSEs.append(MSE)

                # Weight the MSE by the amount of test sessions
                weights.append(len(test))
            
        # Average MSE over all queries
        return np.average(MSEs,weights=weights)




class RelevancePrediction(EvaluationMethod):


    def __init__(self, true_relevances):
        # relevances should be a dict with structure relevances[query_id][region_id][url] -> relevance
        self.relevances = true_relevances
        super(self.__class__, self).__init__()

    def evaluate(self, model, sessions, **kwargs):
        """
            Returns the AUC of the true relevances and the predicted relevances by the model.
            AUC: a statistically consistent and more discriminating measure than accuracy. Charles X. Ling and Jin Huang and Harry Zhang. 2003
        """

        # Get the predicted relevances
        pred_relevances = model.get_relevances(sessions)
        true_relevances = []
        current_i = 0

        # Find the true relevances. If not found throw away the query-document pair
        for session in sessions:
            for rank, result in enumerate(session.web_results):
                if session.query in self.relevances:
                    if result.object in self.relevances[session.query]:
                        true_rel =  self.relevances[session.query][result.object]
                        true_relevances.append(true_rel)
                    else:
                        pred_relevances[current_i] = None
                else:
                    pred_relevances[current_i] = None

                current_i += 1

        # Only use relevances that have a true relevance
        pred_relevances = [rel for rel in pred_relevances if rel]

        assert(len(pred_relevances) == len(true_relevances))
        
        auc = roc_auc_score(true_relevances, pred_relevances)

        return auc


class RankingPerformance(EvaluationMethod):

    def __init__(self, true_relevances):
        # relevances should be a dict with structure relevances[query_id][region_id][url] -> relevanc
        self.relevances = true_relevances
        super(self.__class__, self).__init__()


    def evaluate(self, model, sessions, **kwargs):
        """
            Return the NDCG_5 of the rankings given by the model for the given sessions.
        """
        
        # Only use queries that occur more than MINUMUM_OCCURENCES times and have a true relevance
        counter = collections.Counter([session.query for session in sessions])
        useful_sessions = [query_id for query_id in counter if counter[query_id] >= MINIMUM_OCCURENCES and query_id in self.relevances]
        

        # Group sessions by query
        sessions_dict = dict()
        for session in sessions:
            if session.query in useful_sessions:
                if not session.query in sessions_dict:
                    sessions_dict[session.query] = []
                sessions_dict[session.query].append(session)
        
        total_ndcg = 0
        not_useful = 0

        # For every useful query get the predicted relevance and compute NDCG
        for query_id in useful_sessions:
            
            rel = self.relevances[query_id]
            ideal_ranking = sorted(rel.values(),reverse = True)
            
            # Only use query if there is a document with a positive ranking. (Otherwise IDCG will be 0 -> NDCG undetermined.
            if not all(ideal_ranking):
                not_useful += 1
                continue

            current_sessions = sessions_dict[query_id]
            pred_rels = model.get_relevances(current_sessions)
            i = 0
            
            predicted = dict()

            for session in current_sessions:
                for result in session.web_results:
                    predicted[result.object] = pred_rels[i]
                    i += 1
            
            ranking = sorted([key for key in predicted], key = lambda key : predicted[key],reverse = True)
            rels = []
            broke = False
            for r in ranking[:CUTOFF]:
                try:
                    rels.append( rel[r] )
                except:
                    broke = True
                    break
            
            if broke:
                not_useful += 1
                continue

                                    
            # Compute the NDCG
            dcg = self.dcg(rels[:CUTOFF])
            idcg = self.dcg(ideal_ranking[:CUTOFF])            
            ndcg = dcg / idcg
            
            assert ndcg <= 1

            total_ndcg += ndcg

        
        # If too few queries there might not be any useful queries that also have a ranking in the true_relevances.
        assert not len(useful_sessions)-not_useful is 0
        print len(useful_sessions)-not_useful
        print total_ndcg

         # Average NDCG over all queries
        return total_ndcg / (len(useful_sessions)-not_useful)
            

    def dcg(self, ranking):
        """
            Computes the DCG for a given ranking.
        """
        return sum([(2**r-1)/math.log(i+2,2) for i,r in enumerate(ranking)])
