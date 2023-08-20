import os
import sqlite3
import json
import subprocess
import datetime
import base64
from collections import namedtuple

# pialert modules
import conf
from const import pluginsPath, logPath
from logger import mylog
from helper import timeNowTZ,  updateState, get_file_content, write_file, get_setting, get_setting_value
from api import update_api


#-------------------------------------------------------------------------------
class plugins_state:
    def __init__(self, processScan = False):
        self.processScan = processScan

#-------------------------------------------------------------------------------
def run_plugin_scripts(db, runType, pluginsState = None):

    if pluginsState == None:
        mylog('debug', ['[Plugins] pluginsState initialized '])
        pluginsState = plugins_state()

    # Header
    updateState(db,"Run: Plugins")

    mylog('debug', ['[Plugins] Check if any plugins need to be executed on run type: ', runType])

    for plugin in conf.plugins:

        shouldRun = False        
        prefix = plugin["unique_prefix"]

        set = get_plugin_setting(plugin, "RUN")
        if set != None and set['value'] == runType:
            if runType != "schedule":
                shouldRun = True
            elif  runType == "schedule":
                # run if overdue scheduled time   
                # check schedules if any contains a unique plugin prefix matching the current plugin
                for schd in conf.mySchedules:
                    if schd.service == prefix:          
                        # Check if schedule overdue
                        shouldRun = schd.runScheduleCheck()  

        if shouldRun:            
            # Header
            updateState(db,f"Plugins: {prefix}")
                        
            print_plugin_info(plugin, ['display_name'])
            mylog('debug', ['[Plugins] CMD: ', get_plugin_setting(plugin, "CMD")["value"]])
            pluginsState = execute_plugin(db, plugin, pluginsState) 
            #  update last run time
            if runType == "schedule":
                for schd in conf.mySchedules:
                    if schd.service == prefix:          
                        # note the last time the scheduled plugin run was executed
                        schd.last_run = timeNowTZ()

    return pluginsState





#-------------------------------------------------------------------------------
def get_plugins_configs():
    pluginsList = []  # Create an empty list to store plugin configurations
    
    # Get a list of top-level directories in the specified pluginsPath
    dirs = next(os.walk(pluginsPath))[1]
    
    # Loop through each directory (plugin folder) in dirs
    for d in dirs:
        # Check if the directory name does not start with "__" and does not end with "__ignore"
        if not d.startswith("__") and not d.endswith("__ignore"):
            # Construct the path to the config.json file within the plugin folder
            config_path = os.path.join(pluginsPath, d, "config.json")
            
            # Load the contents of the config.json file as a JSON object and append it to pluginsList
            pluginsList.append(json.loads(get_file_content(config_path)))
    
    return pluginsList  # Return the list of plugin configurations




#-------------------------------------------------------------------------------
def print_plugin_info(plugin, elements = ['display_name']):

    mylog('verbose', ['[Plugins] ---------------------------------------------']) 

    for el in elements:
        res = get_plugin_string(plugin, el)
        mylog('verbose', ['[Plugins] ', el ,': ', res]) 


#-------------------------------------------------------------------------------
# Gets the whole setting object
def get_plugin_setting(plugin, function_key):
    
    result = None

    for set in plugin['settings']:
        if set["function"] == function_key:
          result =  set 

    if result == None:
        mylog('debug', ['[Plugins] Setting with "function":"', function_key, '" is missing in plugin: ', get_plugin_string(plugin, 'display_name')])

    return result


        

#-------------------------------------------------------------------------------
# Get localized string value on the top JSON depth, not recursive
def get_plugin_string(props, el):

    result = ''

    if el in props['localized']:
        for val in props[el]:
            if val['language_code'] == 'en_us':
                result = val['string']
        
        if result == '':
            result = 'en_us string missing'

    else:
        result = props[el]
    
    return result


#-------------------------------------------------------------------------------
# Executes the plugin command specified in the setting with the function specified as CMD 
def execute_plugin(db, plugin, pluginsState = plugins_state() ):
    sql = db.sql  

    # ------- necessary settings check  --------
    set = get_plugin_setting(plugin, "CMD")

    #  handle missing "function":"CMD" setting
    if set == None:                
        return 

    set_CMD = set["value"]

    set = get_plugin_setting(plugin, "RUN_TIMEOUT")

    #  handle missing "function":"<unique_prefix>_TIMEOUT" setting
    if set == None:   
        set_RUN_TIMEOUT = 10
    else:     
        set_RUN_TIMEOUT = set["value"] 

    mylog('debug', ['[Plugins] Timeout: ', set_RUN_TIMEOUT])     

    #  Prepare custom params
    params = []

    if "params" in plugin:
        for param in plugin["params"]:            
            resolved = ""

            #  Get setting value
            if param["type"] == "setting":
                resolved = get_setting(param["value"])

                if resolved != None:
                    resolved = passable_string_from_setting(resolved)

            #  Get Sql result
            if param["type"] == "sql":
                resolved = flatten_array(db.get_sql_array(param["value"]))

            if resolved == None:
                mylog('none', [f'[Plugins] The parameter "name":"{param["name"]}" for "value": {param["value"]} was resolved as None'])

            else:
                params.append( [param["name"], resolved] )
    

    # build SQL query parameters to insert into the DB
    sqlParams = []

    # script 
    if plugin['data_source'] == 'script':
        # ------- prepare params --------
        # prepare command from plugin settings, custom parameters  
        command = resolve_wildcards_arr(set_CMD.split(), params)        

        # Execute command
        mylog('verbose', ['[Plugins] Executing: ', set_CMD])
        mylog('debug',   ['[Plugins] Resolved : ', command])        

        try:
            # try runnning a subprocess with a forced timeout in case the subprocess hangs
            output = subprocess.check_output (command, universal_newlines=True,  stderr=subprocess.STDOUT, timeout=(set_RUN_TIMEOUT))
        except subprocess.CalledProcessError as e:
            # An error occured, handle it
            mylog('none', [e.output])
            mylog('none', ['[Plugins] Error - enable LOG_LEVEL=debug and check logs'])            
        except subprocess.TimeoutExpired as timeErr:
            mylog('none', ['[Plugins] TIMEOUT - the process forcefully terminated as timeout reached']) 


        #  check the last run output
        # Initialize newLines
        newLines = []

        # Create the file path
        file_path = os.path.join(pluginsPath, plugin["code_name"], 'last_result.log')

        # Check if the file exists
        if os.path.exists(file_path):
            # File exists, open it and read its contents
            with open(file_path, 'r+') as f:
                newLines = f.read().split('\n')

            # if the script produced some outpout, clean it up to ensure it's the correct format        
            # cleanup - select only lines containing a separator to filter out unnecessary data
            newLines = list(filter(lambda x: '|' in x, newLines))        
            
            for line in newLines:
                columns = line.split("|")
                # There has to be always 9 columns
                if len(columns) == 9:
                    # Create a tuple containing values to be inserted into the database.
                    # Each value corresponds to a column in the table in the order of the columns.
                    # must match the Plugins_Objects and Plugins_Events databse tables and can be used as input for the plugin_object_class.
                    sqlParams.append(
                        (
                            0,                          # "Index" placeholder
                            plugin["unique_prefix"],    # "Plugin" column value from the plugin dictionary
                            columns[0],                 # "Object_PrimaryID" value from columns list
                            columns[1],                 # "Object_SecondaryID" value from columns list
                            'null',                     # Placeholder for "DateTimeCreated" column
                            columns[2],                 # "DateTimeChanged" value from columns list
                            columns[3],                 # "Watched_Value1" value from columns list
                            columns[4],                 # "Watched_Value2" value from columns list
                            columns[5],                 # "Watched_Value3" value from columns list
                            columns[6],                 # "Watched_Value4" value from columns list
                            'not-processed',            #  "Status" column (placeholder)
                            columns[7],                 # "Extra" value from columns list
                            'null',                     # Placeholder for "UserData" column
                            columns[8]                  # "ForeignKey" value from columns list
                        )
                    )
                else:
                    mylog('none', ['[Plugins] Skipped invalid line in the output: ', line])
        else:
            mylog('debug', [f'[Plugins] The file {file_path} does not exist'])             
    
    # pialert-db-query
    if plugin['data_source'] == 'pialert-db-query':
        # replace single quotes wildcards
        q = set_CMD.replace("{s-quote}", '\'')

        # Execute command
        mylog('verbose', ['[Plugins] Executing: ', q])

        # set_CMD should contain a SQL query        
        arr = db.get_sql_array (q) 

        for row in arr:
            # There has to be always 9 columns
            if len(row) == 9 and (row[0] in ['','null']) == False :
                # Create a tuple containing values to be inserted into the database.
                # Each value corresponds to a column in the table in the order of the columns.
                # must match the Plugins_Objects and Plugins_Events databse tables and can be used as input for the plugin_object_class
                sqlParams.append(
                    (
                        0,                          # "Index" placeholder
                        plugin["unique_prefix"],    # "Plugin" plugin dictionary
                        row[0],                     # "Object_PrimaryID" row
                        handle_empty(row[1]),       # "Object_SecondaryID" column after handling empty values
                        'null',                     # Placeholder "DateTimeCreated" column
                        row[2],                     # "DateTimeChanged" row
                        row[3],                     # "Watched_Value1" row
                        row[4],                     # "Watched_Value2" row
                        handle_empty(row[5]),       # "Watched_Value3" column after handling empty values
                        handle_empty(row[6]),       # "Watched_Value4" column after handling empty values
                        'not-processed',            #  "Status" column (placeholder)
                        row[7],                     # "Extra" row
                        'null',                     # Placeholder "UserData" column
                        row[8]                      # "ForeignKey" row
                    )
                )
            else:
                mylog('none', ['[Plugins] Skipped invalid sql result'])
    
    # pialert-db-query
    if plugin['data_source'] == 'sqlite-db-query':
        # replace single quotes wildcards
        # set_CMD should contain a SQL query    
        q = set_CMD.replace("{s-quote}", '\'')

        # Execute command
        mylog('verbose', ['[Plugins] Executing: ', q])

        # ------- necessary settings check  --------
        set = get_plugin_setting(plugin, "DB_PATH")

        #  handle missing "function":"DB_PATH" setting
        if set == None:                
            mylog('none', ['[Plugins] Error: DB_PATH setting for plugin type sqlite-db-query missing.'])
            return 
        
        fullSqlitePath = set["value"]

        #  try attaching the sqlite DB
        try:
            sql.execute ("ATTACH DATABASE '"+ fullSqlitePath +"' AS EXTERNAL")
        except sqlite3.Error as e:
            mylog('none',[ '[Plugin] - ATTACH DATABASE failed with SQL ERROR: ', e])

        arr = db.get_sql_array (q) 

        for row in arr:
            # There has to be always 9 columns
            if len(row) == 9 and (row[0] in ['','null']) == False :
                # Create a tuple containing values to be inserted into the database.
                # Each value corresponds to a column in the table in the order of the columns.
                # must match the Plugins_Objects and Plugins_Events databse tables and can be used as input for the plugin_object_class
                sqlParams.append(
                    0,                            #  "Index" placeholder
                    (plugin["unique_prefix"],     #  "Plugin" 
                    row[0],                       #  "Object_PrimaryID" 
                    handle_empty(row[1]),         #  "Object_SecondaryID" 
                    'null',                       #  "DateTimeCreated" column (null placeholder)
                    row[2],                       #  "DateTimeChanged" 
                    row[3],                       #  "Watched_Value1" 
                    row[4],                       #  "Watched_Value2" 
                    handle_empty(row[5]),         #  "Watched_Value3" 
                    handle_empty(row[6]),         #  "Watched_Value4" 
                    'not-processed',              #  "Status" column (placeholder)
                    row[7],                       #  "Extra" 
                    'null',                       #  "UserData" column (null placeholder)
                    row[8]))                      #  "ForeignKey" 
            else:
                mylog('none', ['[Plugins] Skipped invalid sql result'])


    # check if the subprocess / SQL query failed / there was no valid output
    if len(sqlParams) == 0: 
        mylog('none', ['[Plugins] No output received from the plugin ', plugin["unique_prefix"], ' - enable LOG_LEVEL=debug and check logs'])
        return  
    else: 
        mylog('verbose', ['[Plugins] SUCCESS, received ', len(sqlParams), ' entries'])  

    # process results if any
    if len(sqlParams) > 0:                

        try:
            sql.executemany("""INSERT INTO Plugins_History ("Index", "Plugin", "Object_PrimaryID", "Object_SecondaryID", "DateTimeCreated", "DateTimeChanged", "Watched_Value1", "Watched_Value2", "Watched_Value3", "Watched_Value4", "Status" ,"Extra", "UserData", "ForeignKey") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", sqlParams)
            db.commitDB()
        except sqlite3.Error as e:
            db.rollbackDB()  # Rollback changes in case of an error
            mylog('none', ['[Plugins] ERROR inserting into Plugins_History:', e]) 
            

        # create objects
        pluginsState = process_plugin_events(db, plugin, pluginsState, sqlParams)

        # update API endpoints
        update_api(db, False, ["plugins_events","plugins_objects"])  
    
    return pluginsState

#-------------------------------------------------------------------------------
def custom_plugin_decoder(pluginDict):
    return namedtuple('X', pluginDict.keys())(*pluginDict.values())        

#-------------------------------------------------------------------------------
# Handle empty value 
def handle_empty(value):    
    if value == '' or value is None:
        value = 'null'

    return value    


#-------------------------------------------------------------------------------
# Flattens a setting to make it passable to a script
def passable_string_from_setting(globalSetting):

    setVal = globalSetting[6] # setting value
    setTyp = globalSetting[3] # setting type

    noConversion = ['text', 'string', 'integer', 'boolean', 'password', 'readonly', 'integer.select', 'text.select', 'integer.checkbox'  ]
    arrayConversion = ['text.multiselect', 'list'] 
    arrayConversionBase64 = ['subnets'] 
    jsonConversion = ['.template'] 

    mylog('debug', f'[Plugins] setTyp: {setTyp}')

    if setTyp in noConversion:
        return setVal

    if setTyp in arrayConversion:
        return flatten_array(setVal)

    if setTyp in arrayConversionBase64:
        
        return flatten_array(setVal, encodeBase64 = True)

    for item in jsonConversion:
        if setTyp.endswith(item):
            return json.dumps(setVal)

    mylog('none', ['[Plugins] ERROR: Parameter not converted.'])  



#-------------------------------------------------------------------------------
# Gets the setting value
def get_plugin_setting_value(plugin, function_key):
    
    resultObj = get_plugin_setting(plugin, function_key)

    if resultObj != None:
        return resultObj["value"]

    return None



#-------------------------------------------------------------------------------
def flatten_array(arr, encodeBase64=False):
    tmp = ''
    arrayItemStr = ''

    mylog('debug', '[Plugins] Flattening the below array')
    mylog('debug', f'[Plugins] Convert to Base64: {encodeBase64}')
    mylog('debug', arr)

    for arrayItem in arr:
        # only one column flattening is supported
        if isinstance(arrayItem, list):            
            arrayItemStr = str(arrayItem[0]).replace("'", '')  # removing single quotes - not allowed            
        else:
            # is string already
            arrayItemStr = arrayItem


        tmp += f'{arrayItemStr},'

    tmp = tmp[:-1]  # Remove last comma ','

    mylog('debug', f'[Plugins] Flattened array: {tmp}')

    if encodeBase64:
        tmp = str(base64.b64encode(tmp.encode('ascii')))
        mylog('debug', f'[Plugins] Flattened array (base64): {tmp}')


    return tmp



#-------------------------------------------------------------------------------
# Replace {wildcars} with parameters
def resolve_wildcards_arr(commandArr, params):

    mylog('debug', ['[Plugins] Pre-Resolved CMD: '] + commandArr)   

    for param in params:
        # mylog('debug', ['[Plugins] key     : {', param[0], '}'])
        # mylog('debug', ['[Plugins] resolved: ', param[1]])

        i = 0
        
        for comPart in commandArr:

            commandArr[i] = comPart.replace('{' + param[0] + '}', param[1]).replace('{s-quote}',"'")

            i += 1

    return commandArr    


#-------------------------------------------------------------------------------
# Combine plugin objects, keep user-defined values, created time, changed time if nothing changed and the index
def combine_plugin_objects(old, new):    
    
    new.userData = old.userData 
    new.index = old.index 
    new.created = old.created 

    # Keep changed time if nothing changed
    if new.status in ['watched-not-changed']:
        new.changed = old.changed

    #  return the new object, with some of the old values
    return new

#-------------------------------------------------------------------------------
# Check if watched values changed for the given plugin
def process_plugin_events(db, plugin, pluginsState, plugEventsArr):    
    sql = db.sql

    # Access the connection from the DB instance
    conn = db.sql_connection

    pluginPref = plugin["unique_prefix"]

    mylog('debug', ['[Plugins] Processing        : ', pluginPref])

    try:
        # Begin a transaction
        with conn:

            #  get existing objects
            plugObjectsArr = db.get_sql_array ("SELECT * FROM Plugins_Objects where Plugin = '" + str(pluginPref)+"'") 

            pluginObjects = []
            pluginEvents  = []

            for obj in plugObjectsArr: 
                pluginObjects.append(plugin_object_class(plugin, obj))

            existingPluginObjectsCount = len(pluginObjects)

            mylog('debug', ['[Plugins] Existing objects        : ', existingPluginObjectsCount])
            mylog('debug', ['[Plugins] New and existing events : ', len(plugEventsArr)])

            
            # create plugin objects from events - will be processed to find existing objects 
            for eve in plugEventsArr:                                  
                pluginEvents.append(plugin_object_class(plugin, eve))

            
            #  Loop thru all current events and update the status to "exists" if the event matches an existing object
            index = 0
            for tmpObjFromEvent in pluginEvents:        

                #  compare hash of the IDs for uniqueness
                if any(x.idsHash == tmpObjFromEvent.idsHash for x in pluginObjects):                    
            
                    pluginEvents[index].status = "exists"            
                index += 1


            # Loop thru events and check if the ones that exist have changed in the watched columns
            # if yes update status accordingly
            index = 0
            for tmpObjFromEvent in pluginEvents:  

                if tmpObjFromEvent.status == "exists": 

                    #  compare hash of the changed watched columns for uniqueness
                    if any(x.watchedHash != tmpObjFromEvent.watchedHash for x in pluginObjects):
                        pluginEvents[index].status = "watched-changed"                            
                    else:
                        pluginEvents[index].status = "watched-not-changed"  
                index += 1

            # Loop thru events and check if previously available objects are missing
            # Create a set of hashes of IDs in pluginObjects
            plugin_objects_ids_hash = set(x.idsHash for x in pluginObjects)

            for tmpObjFromEvent in pluginEvents:
                if tmpObjFromEvent.idsHash not in plugin_objects_ids_hash:
                    for x in pluginObjects:
                        if x.primaryId == tmpObjFromEvent.primaryId and x.secondaryId == tmpObjFromEvent.secondaryId:
                            mylog('debug', ['[Plugins] Missing from last scan: ', x.primaryId , x.secondaryId])
                            x.status  = "missing-in-last-scan"
                            x.changed = datetime.now(timeZone).strftime("%Y-%m-%d %H:%M:%S")
                            break


            # Merge existing plugin objects with newly discovered ones and update existing ones with new values
            for tmpObjFromEvent in pluginEvents:
                # set "new" status for new objects and append
                if tmpObjFromEvent.status == 'not-processed':
                    # This is a new object as it was not discovered as "exists" previously                    
                    tmpObjFromEvent.status = 'new'

                    pluginObjects.append(tmpObjFromEvent)
                # update data of existing objects
                else:
                    index = 0
                    for plugObj in pluginObjects:
                        # find corresponding object for the event and merge
                        if plugObj.idsHash == tmpObjFromEvent.idsHash:               
                            pluginObjects[index] =  combine_plugin_objects(plugObj, tmpObjFromEvent)                    

                        index += 1

            # Update the DB
            # ----------------------------
            # Update the Plugin_Objects    
            # Create lists to hold the data for bulk insertion
            objects_to_insert = []
            events_to_insert = []
            objects_to_update = []

            statuses_to_report_on = get_plugin_setting_value(plugin, "REPORT_ON")

            mylog('debug', ['[Plugins] statuses_to_report_on: ', statuses_to_report_on])

            for plugObj in pluginObjects:
                #  keep old createdTime time if the plugObj already was created before
                createdTime = plugObj.changed if plugObj.status == 'new' else plugObj.created
                #  13 values without Index
                values = (
                    plugObj.pluginPref, plugObj.primaryId, plugObj.secondaryId, createdTime,
                    plugObj.changed, plugObj.watched1, plugObj.watched2, plugObj.watched3,
                    plugObj.watched4, plugObj.status, plugObj.extra, plugObj.userData,
                    plugObj.foreignKey
                )

                if plugObj.status == 'new':
                    objects_to_insert.append(values)
                else:
                    objects_to_update.append(values + (plugObj.index,))  # Include index for UPDATE

                # only generate events that we want to be notified on
                if plugObj.status in statuses_to_report_on:
                    events_to_insert.append(values)

            mylog('debug', ['[Plugins] events_to_insert count: ', len(events_to_insert)])
            mylog('debug', ['[Plugins] pluginEvents count : ', len(pluginEvents)])
            logEventStatusCounts(pluginEvents)
            mylog('debug', ['[Plugins] pluginObjects count: ', len(pluginObjects)])
            logEventStatusCounts(pluginObjects)

            # Bulk insert objects
            if objects_to_insert:
                sql.executemany(
                    """
                    INSERT INTO Plugins_Objects 
                    ("Plugin", "Object_PrimaryID", "Object_SecondaryID", "DateTimeCreated", 
                    "DateTimeChanged", "Watched_Value1", "Watched_Value2", "Watched_Value3", 
                    "Watched_Value4", "Status", "Extra", "UserData", "ForeignKey") 
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, objects_to_insert
                )

            # Bulk update objects
            if objects_to_update:
                sql.executemany(
                    """
                    UPDATE Plugins_Objects
                    SET "Plugin" = ?, "Object_PrimaryID" = ?, "Object_SecondaryID" = ?, "DateTimeCreated" = ?, 
                        "DateTimeChanged" = ?, "Watched_Value1" = ?, "Watched_Value2" = ?, "Watched_Value3" = ?, 
                        "Watched_Value4" = ?, "Status" = ?, "Extra" = ?, "UserData" = ?, "ForeignKey" = ?
                    WHERE "Index" = ?
                    """, objects_to_update
                )

            # Bulk insert events
            if events_to_insert:
                sql.executemany(
                    """
                    INSERT INTO Plugins_Events 
                    ("Plugin", "Object_PrimaryID", "Object_SecondaryID", "DateTimeCreated", 
                    "DateTimeChanged", "Watched_Value1", "Watched_Value2", "Watched_Value3", 
                    "Watched_Value4", "Status", "Extra", "UserData", "ForeignKey") 
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, events_to_insert
                )

            # Commit changes to the database
            db.commitDB()



    except Exception as e:
        # Rollback the transaction in case of an error
        conn.rollback()
        mylog('none', ['[Plugins] SQL transaction error: ', e])
        raise e   

    # Perform database table mapping if enabled for the plugin   
    if len(pluginEvents) > 0 and  "mapped_to_table" in plugin:

        # Initialize an empty list to store SQL parameters.
        sqlParams = []

        # Get the database table name from the 'mapped_to_table' key in the 'plugin' dictionary.
        dbTable = plugin['mapped_to_table']

        # Log a debug message indicating the mapping of objects to the database table.
        mylog('debug', ['[Plugins] Mapping objects to database table: ', dbTable])

        # Initialize lists to hold mapped column names, columnsStr, and valuesStr for SQL query.
        mappedCols = []
        columnsStr = ''
        valuesStr = ''

        # Loop through the 'database_column_definitions' in the 'plugin' dictionary to collect mapped columns.
        # Build the columnsStr and valuesStr for the SQL query.
        for clmn in plugin['database_column_definitions']:
            if 'mapped_to_column' in clmn:
                mappedCols.append(clmn)
                columnsStr = f'{columnsStr}, "{clmn["mapped_to_column"]}"'
                valuesStr = f'{valuesStr}, ?'

        # Remove the first ',' from columnsStr and valuesStr.
        if len(columnsStr) > 0:
            columnsStr = columnsStr[1:]
            valuesStr = valuesStr[1:]

        # Map the column names to plugin object event values and create a list of tuples 'sqlParams'.
        for plgEv in pluginEvents:
            tmpList = []

            for col in mappedCols:
                if col['column'] == 'Index':
                    tmpList.append(plgEv.index)
                elif col['column'] == 'Plugin':
                    tmpList.append(plgEv.pluginPref)
                elif col['column'] == 'Object_PrimaryID':
                    tmpList.append(plgEv.primaryId)
                elif col['column'] == 'Object_SecondaryID':
                    tmpList.append(plgEv.secondaryId)
                elif col['column'] == 'DateTimeCreated':
                    tmpList.append(plgEv.created)
                elif col['column'] == 'DateTimeChanged':
                    tmpList.append(plgEv.changed)
                elif col['column'] == 'Watched_Value1':
                    tmpList.append(plgEv.watched1)
                elif col['column'] == 'Watched_Value2':
                    tmpList.append(plgEv.watched2)
                elif col['column'] == 'Watched_Value3':
                    tmpList.append(plgEv.watched3)
                elif col['column'] == 'Watched_Value4':
                    tmpList.append(plgEv.watched4)
                elif col['column'] == 'UserData':
                    tmpList.append(plgEv.userData)
                elif col['column'] == 'Extra':
                    tmpList.append(plgEv.extra)
                elif col['column'] == 'Status':
                    tmpList.append(plgEv.status)

            # Append the mapped values to the list 'sqlParams' as a tuple.
            sqlParams.append(tuple(tmpList))

        # Generate the SQL INSERT query using the collected information.
        q = f'INSERT into {dbTable} ({columnsStr}) VALUES ({valuesStr})'

        # Log a debug message showing the generated SQL query for mapping.
        mylog('debug', ['[Plugins] SQL query for mapping: ', q])

        # Execute the SQL query using 'sql.executemany()' and the 'sqlParams' list of tuples.
        # This will insert multiple rows into the database in one go.
        sql.executemany(q, sqlParams)

        db.commitDB()

        # perform scan if mapped to CurrentScan table        
        if dbTable == 'CurrentScan':               
            pluginsState.processScan = True  
 

    db.commitDB()

    return pluginsState


#-------------------------------------------------------------------------------
class plugin_object_class:
    def __init__(self, plugin, objDbRow):
        self.index        = objDbRow[0]
        self.pluginPref   = objDbRow[1]
        self.primaryId    = objDbRow[2]
        self.secondaryId  = objDbRow[3]
        self.created      = objDbRow[4] # can be null
        self.changed      = objDbRow[5] # never null (data coming from plugin)
        self.watched1     = objDbRow[6]
        self.watched2     = objDbRow[7]
        self.watched3     = objDbRow[8]
        self.watched4     = objDbRow[9]
        self.status       = objDbRow[10]
        self.extra        = objDbRow[11]
        self.userData     = objDbRow[12]
        self.foreignKey   = objDbRow[13]

        # Check if self.status is valid
        if self.status not in ["exists", "watched-changed", "watched-not-changed", "new", "not-processed", "missing-in-last-scan"]:
            raise ValueError("Invalid status value for plugin object:", self.status)

        # self.idsHash      = str(hash(str(self.primaryId) + str(self.secondaryId)))    
        self.idsHash      = str(self.primaryId) + str(self.secondaryId)

        self.watchedClmns = []
        self.watchedIndxs = []          

        setObj = get_plugin_setting(plugin, 'WATCH')

        indexNameColumnMapping = [(6, 'Watched_Value1' ), (7, 'Watched_Value2' ), (8, 'Watched_Value3' ), (9, 'Watched_Value4' )]

        if setObj is not None:            
            
            self.watchedClmns = setObj["value"]

            for clmName in self.watchedClmns:
                for mapping in indexNameColumnMapping:
                    if clmName == indexNameColumnMapping[1]:
                        self.watchedIndxs.append(indexNameColumnMapping[0])

        tmp = ''
        for indx in self.watchedIndxs:
            tmp += str(objDbRow[indx])

        self.watchedHash  = str(hash(tmp))

#-------------------------------------------------------------------------------
def logEventStatusCounts(pluginEvents):
    status_counts = {}  # Dictionary to store counts for each status

    for event in pluginEvents:
        status = event.status
        if status in status_counts:
            status_counts[status] += 1
        else:
            status_counts[status] = 1

    for status, count in status_counts.items():
        mylog('debug', [f'Status "{status}": {count} events'])