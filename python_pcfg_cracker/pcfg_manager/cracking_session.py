#!/usr/bin/env python3

########################################################################################
#
# Name: PCFG_Cracker Cracking Management Code
# Description: Manages a cracking session
#              Eventaully I want to add other functionality into the pcfg_manager codebase
#              such as honeywords, so I am moving the actually running of a cracking
#              session into this chunk of code
#
#########################################################################################

import sys
import time
import threading ##--Used only for the "check for user input" threads
from multiprocessing import Process, Queue, Pipe

from pcfg_manager.core_grammar import PcfgClass, print_grammar
from pcfg_manager.priority_queue import PcfgQueue
from pcfg_manager.queue_storage import QueueStorage
from pcfg_manager.ret_types import RetType


###################################################################################################
# Used to manage a password cracking session
###################################################################################################
class CrackingSession:
    
    ############################################################################
    # Basic initialization function
    ############################################################################
    def __init__(self, pcfg = None, verbose = False):
        self.pcfg = pcfg
        
        ##--Debugging and Performance Monitoring Variables--##
        self.num_parse_trees = 0      #-Total number of parse_trees processed so far
        self.num_guesses = 0          #-Total number of guesses made so far
        self.p_queue_start_time = 0   #-Start time of running the Next algorithm on the priority queue
        self.p_queue_stop_time = 0    #-Stop time of running the Next algorithm
        self.guess_start_time = 0     #-Start time of genering the actual guesses
        self.guess_stop_time = 0      #-Stop time of generating the actual guesses
        self.running_queue_time = 0   #-Total time running the "Next" algorithm to get a new pre-terminal
        self.running_guess_time = 0   #-Total time spent generaing guesses from pre-terminals and printing them out
        self.verbose = verbose

    ##############################################################################
    # Starts the cracking session and starts generating guesses
    ##############################################################################
    def run(self, print_queue_info = False):
        #-Start the clock
        self.total_time_start = time.perf_counter()
        
        #-Still going back and forth on if I should use a pipe or queue here
        pqueue_to_manager = Queue(maxsize = 10000)
        
        ##--Spawn a child process to handle backup storage as the list gets too big for the main priority queue
        #-Initialize the data structures
        backup_storage = QueueStorage(verbose = self.verbose)
        #-At least from my testing, queue while slower in general was signifincatly faster than pipes for transfering a lot of data
        #-in a non-blocking way
        backup_save_comm = Queue()
        backup_restore_comm = Queue()
        #-Now create and start the process
        backup_storage_process = Process(target=backup_storage.start_process, args=(backup_save_comm, backup_restore_comm))
        backup_storage_process.daemon = True
        backup_storage_process.start()
        
        #-Spawn a child process to start generating the pre-terminals
        priority_queue_process = Process(target=spawn_pqueue_thread, args=(self.pcfg, pqueue_to_manager, self.verbose, print_queue_info, backup_save_comm, backup_restore_comm))
        priority_queue_process.daemon = True
        priority_queue_process.start()
        
        ##--Setup the check to see if a user is pressing a button--#
        user_input = [None]
        user_thread = threading.Thread(target=keypress, args=(user_input,))
        user_thread.daemon = True  # thread dies when main thread (only non-daemon thread) exits.
        user_thread.start()
        
                
        self.p_queue_start_time = time.perf_counter()
        
        #-Get the first item from the child priority_queue process
        queue_item = pqueue_to_manager.get()
        
        #-If the value None is encountered the queue is either empty or an error occured so stopped
        if queue_item is None:
            print("Finished processing items from the priority queue", file=sys.stderr)
            priority_queue_process.join()
            return RetType.QUEUE_EMPTY
                    
        self.p_queue_stop_time = time.perf_counter() - self.p_queue_start_time
        self.running_queue_time = self.running_queue_time + self.p_queue_stop_time
        
        ##--Keep running while the p_queue.next_function still has items in it
        while True:
            
            ##--Expand the guesses from the parse tree
            self.guess_start_time = time.perf_counter()
            current_guesses = self.pcfg.list_terminals(queue_item.parse_tree) 
            self.guess_stop_time = time.perf_counter() - self.guess_start_time
            self.running_guess_time = self.running_guess_time + self.guess_stop_time
            
            self.num_parse_trees = self.num_parse_trees +1
            self.num_guesses = self.num_guesses + len(current_guesses) 
            
            ##--Print_Queue_Info says if we are running this session for debugging and performance improvements vs actually cracking passwords
            if print_queue_info == True:     
                
                if self.num_parse_trees % 10000 == 0:
                    print ("PQueueTime " + str(self.running_queue_time),file=sys.stderr)
                    print ("Guesses:" + str(self.num_guesses),file=sys.stderr)
                    print ("GuessTime " + str(self.running_guess_time),file=sys.stderr)
                    print ("Average num of guesses per parse-tree: " + str(self.num_guesses // self.num_parse_trees),file=sys.stderr)
                    print ("Total Time " + str(time.perf_counter() - self.total_time_start),file=sys.stderr)
                    print ("Number of guesses a second: " + str(self.num_guesses // (time.perf_counter() - self.total_time_start)),file=sys.stderr)
                    #print ("Current probability: " + str(self.p_queue.max_probability),file=sys.stderr)
                    print ()

            ##--This is if you are actually trying to generate guesses
            else:
                for guess in current_guesses:
                    try:
                        print(guess)
                    ##--While I could silently replace/ignore the Unicode character for now I want to know if this is happening
                    except UnicodeEncodeError:
                        print("UNICODE_ERROR",file=sys.stderr)       
                    except IOError:
                        print("Consumer, (probably the password cracker), stopped accepting input.",file=sys.stderr)
                        print("Halting guess generation and exiting",file=sys.stderr)
                        return RetType.BROKEN_PIPE
            
            ##--Check for user requested status output--##
            if user_input[0] is not None:          
                self.display_status(guess_list = current_guesses)
                user_input[0] = None
                ##--Kick off again the thread to check if user_input was entered
                if not user_thread.is_alive():
                    user_thread = threading.Thread(target=keypress, args=(user_input,))
                    user_thread.daemon = True  # thread dies when main thread (only non-daemon thread) exits.
                    user_thread.start()
                    
            ##--Generate more parse trees from the priority queue
            self.p_queue_start_time = time.perf_counter()
            
            queue_item = pqueue_to_manager.get()
        
            #-If the value None is encountered the queue is either empty or an error occured so stopped
            if queue_item is None:
                print("Finished processing items from the priority queue", file=sys.stderr)
                priority_queue_process.join()
                return RetType.QUEUE_EMPTY
                
            self.p_queue_stop_time = time.perf_counter() - self.p_queue_start_time
            self.running_queue_time = self.running_queue_time + self.p_queue_stop_time         
                
        return RetType.STATUS_OK
    

    ######################################################################################################
    # Displays status of cracking session
    ######################################################################################################
    def display_status(self, guess_list = []):
        print ("Status Report:",file=sys.stderr)
        if len(guess_list) != 0:
            print ("Currently generating guesses from " + str(guess_list[0]) + " to " + str(guess_list[-1]),file=sys.stderr)
        print("",file=sys.stderr)
        return RetType.STATUS_OK
   

###########################################################################################
# Used to check to see if a key was pressed to output program status
# *Hopefully* should work on multiple OSs
# --Simply check user_input_char to see if it is not none
###########################################################################################
def keypress(user_input_ref):
    user_input_ref[0] = input()


###############################################################################################
# The child process that is spawned off to run the priority queue and generate parse trees to
# send to the parent process
# The parse trees will be sent back to the parent process in priority order
###############################################################################################
def spawn_pqueue_thread(pcfg, pqueue_to_manager, verbose, print_queue_info, backup_save_comm, backup_restore_comm):
        
    ##--Initialize the priority queue--##
    p_queue = PcfgQueue(backup_save_comm, backup_restore_comm, verbose = verbose)
    ret_value = p_queue.initialize(pcfg)
    if ret_value != RetType.STATUS_OK:
        print ("Error initalizing the priority queue, exiting",file=sys.stderr)
        return ret_value 
        
    ##--Now start generating parse trees to send back to the main process
    num_parse_trees = 0
    queue_item_list = []
    ret_value = p_queue.next_function(pcfg, queue_item_list)
    
    ##--There are no more items to use in the queue, let the parent know we are done
    if ret_value == RetType.QUEUE_EMPTY:
        pqueue_to_manager.put(None)
        return ret_value
    
    while ret_value == RetType.STATUS_OK:
        for i in queue_item_list:
            pqueue_to_manager.put(i)
            num_parse_trees = num_parse_trees + 1
            
            ##--Print out debugging info if requested
            if print_queue_info == True:
                if num_parse_trees % 10000 == 0:
                    print ("Total number of Parse Trees: " + str (num_parse_trees),file=sys.stderr)
                    print ("PQueue:" + str(len(p_queue.p_queue)),file=sys.stderr)
                    print ("messaging size: " + str(pqueue_to_manager.qsize()))
                    ##--Request and get the size of the backup storage list
                    backup_save_comm.put({"Command":"Status"})
                    result = backup_restore_comm.get()
                    print ("Backup storage list:" + str(result['Size']),file=sys.stderr)
            
        queue_item_list = []
        ret_value = p_queue.next_function(pcfg, queue_item_list)
        ##--There are no more items to use in the queue, let the parent know we are done
        if ret_value == RetType.QUEUE_EMPTY:
            pqueue_to_manager.put(None)
            return ret_value
        
    return ret_value