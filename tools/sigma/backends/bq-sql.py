# Output backends for sigmac
# Copyright 2019 Jayden Zheng
# Copyright 2020 Jonas Hagg
# Copyright 2021 wagga (https://github.com/wagga40/)

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.

# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import re
import sigma
from sigma.backends.base import SingleTextQueryBackend
from sigma.parser.condition import SigmaAggregationParser, NodeSubexpression, ConditionAND, ConditionOR, ConditionNOT
from sigma.parser.exceptions import SigmaParseError
class SQLBackend(SingleTextQueryBackend):
    """Converts Sigma rule into SQL query"""
    identifier = "bq-sql"
    active = True

    andToken = " AND "                      # Token used for linking expressions with logical AND
    orToken = " OR "                        # Same for OR
    notToken = "NOT "                       # Same for NOT
    subExpression = "(%s)"                  # Syntax for subexpressions, usually parenthesis around it. %s is inner expression
    listExpression = "(%s)"                 # Syntax for lists, %s are list items separated with listSeparator
    listSeparator = ", "                    # Character for separation of list items
    valueExpression = "\"%s\""              # Expression of values, %s represents value
    nullExpression = "-%s=*"                # Expression of queries for null values or non-existing fields. %s is field name
    notNullExpression = "%s=*"              # Expression of queries for not null values. %s is field name
    mapExpression = "%s = %s"               # Syntax for field/value conditions. First %s is fieldname, second is value
    mapMulti = "%s IN %s"                   # Syntax for field/value conditions. First %s is fieldname, second is value
    mapWildcard = "LOWER(%s) LIKE %s"# Syntax for swapping wildcard conditions: Adding \ as escape character
    mapSource = "%s=%s"                     # Syntax for sourcetype
    mapListsSpecialHandling = False         # Same handling for map items with list values as for normal values (strings, integers) if True, generateMapItemListNode method is called with node
    mapListValueExpression = "%s OR %s"     # Syntax for field/value condititons where map value is a list
    mapLength = "(%s %s)"

    options = SingleTextQueryBackend.options + (
        ("table", "eventlog", "Use this option to specify table name.", None),
        ("select", "*", "Use this option to specify fields you want to select. Example: \"--backend-option select=xxx,yyy\"", None),
        ("selection", False, "Use this option to enable fields selection from Sigma rules.", None),
        ("type1", False, "Use this option to enable type selection from Sigma rules.", None)
    )

    selection_enabled = False
    

    def __init__(self, sigmaconfig, options):
        super().__init__(sigmaconfig)
        
        if "table" in options:
            self.table = options["table"]
        else:
            self.table = "<org>"

        if "select" in options and options["select"]:
            self.select_fields = options["select"].split(',')
        else:
            self.select_fields = list()

        if "selection" in options:
            self.selection_enabled = True

        if "type1" in options and options["type1"]:
            self.type1_fields = options["type1"].split(',')
        else:
            self.type1_fields = list()

    def generateANDNode(self, node):
        generated = [ self.generateNode(val) for val in node ]
        filtered = [ g for g in generated if g is not None ]
        if filtered:
            return self.andToken.join(filtered)
        else:
            return None

    def generateORNode(self, node):
        generated = [ self.generateNode(val) for val in node ]
        filtered = [ g for g in generated if g is not None ]
        if filtered:
            return self.orToken.join(filtered)
        else:
            return None

    def generateNOTNode(self, node):
        generated = self.generateNode(node.item)
        if generated is not None:
            return self.notToken + generated
        else:
            return None

    def generateSubexpressionNode(self, node):
        generated = self.generateNode(node.items)
        if generated:
            return self.subExpression % generated
        else:
            return None

    def generateListNode(self, node):
        if not set([type(value) for value in node]).issubset({str, int}):
            raise TypeError("List values must be strings or numbers")
        return self.listExpression % (self.listSeparator.join([self.generateNode(value) for value in node]))

    def generateMapItemNode(self, node):
        fieldname, value = node
        transformed_fieldname = self.fieldNameMapping(fieldname, value)
        generated_value = self.generateNode(value)

        has_wildcard = re.search(r"((\\(\*|\?|\\))|\*|\?|_|%)", generated_value)

        if "," in generated_value  and generated_value[0]=="(" and generated_value[-1]==")" and not has_wildcard:
            return self.mapMulti % (transformed_fieldname, generated_value)
        elif "LENGTH" in transformed_fieldname:
            return self.mapLength % (transformed_fieldname, value)
        elif type(value) == list:
            return self.generateMapItemListNode(transformed_fieldname, value)
        elif self.mapListsSpecialHandling == False and type(value) in (str, int, list) or self.mapListsSpecialHandling == True and type(value) in (str, int):
            if has_wildcard:
                return self.mapWildcard % (transformed_fieldname, generated_value)
            else:
                return self.mapExpression % (transformed_fieldname, generated_value)
        elif "sourcetype" in transformed_fieldname:
            return self.mapSource % (transformed_fieldname, generated_value)
        elif has_wildcard:
            return self.mapWildcard % (transformed_fieldname, generated_value)
        else:
            raise TypeError("Backend does not support map values of type " + str(type(value)))

    def generateMapItemListNode(self, key, value):
        return "(" + (" OR ".join([self.mapWildcard % (key, self.generateValueNode(item)) for item in value])) + ")"

    def generateValueNode(self, node):
            return self.valueExpression % (self.cleanValue(str(node)))

    def generateNULLValueNode(self, node):
        return self.nullExpression % (node.item)

    def generateNotNULLValueNode(self, node):
        return self.notNullExpression % (node.item)

    def fieldNameMapping(self, fieldname, value):
        """
        Alter field names depending on the value(s). Backends may use this method to perform a final transformation of the field name
        in addition to the field mapping defined in the conversion configuration. The field name passed to this method was already
        transformed from the original name given in the Sigma rule.
        """
        return fieldname

    def generate(self, sigmaparser):
        """Method is called for each sigma rule and receives the parsed rule (SigmaParser)"""
        fields = list()

        # First add fields specified in the rule
        try:
            for field in sigmaparser.parsedyaml["fields"]:
                mapped = sigmaparser.config.get_fieldmapping(field).resolve_fieldname(field, sigmaparser)
                if type(mapped) == str:
                    fields.append(mapped)
                elif type(mapped) == list:
                    fields.extend(mapped)
                else:
                    raise TypeError("Field mapping must return string or list")

        except KeyError:    # no 'fields' attribute
            pass

        # Then add fields specified in the backend configuration
        fields.extend(self.select_fields)

        # In case select is specified in backend option, we want to enable selection
        if len(self.select_fields) > 0:
            self.selection_enabled = True

        # Finally, in case fields is empty, add the default value
        if not fields:
            fields = list("*")

        for parsed in sigmaparser.condparsed:
            if self.selection_enabled:
                query = self._generateQueryWithFields(parsed, fields)
            else:
                query = self.generateQuery(parsed)
            before = self.generateBefore(parsed)
            after = self.generateAfter(parsed)

            result = ""
            if before is not None:
                result = before
            if query is not None:
                result += query
            if after is not None:
                result += after

            return result

    def cleanValue(self, val):
        if not isinstance(val, str):
            return str(val)

        #Single backlashes which are not in front of * or ? are doulbed
        val = re.sub(r"(?<!\\)\\(?!(\\|\*|\?))", r"\\\\\\\\", val)

        #Replace _ with \_ because _ is a sql wildcard
        val = re.sub(r'_', r'\_', val)

        #Replace % with \% because % is a sql wildcard
        val = re.sub(r'%', r'\%', val)

        #Replace * with %, if even number of backslashes (or zero) in front of *
        val = re.sub(r"(?<!\\)(\\\\)*(?!\\)\*", r"\1%", val)

        #Replace ? with _, if even number of backsashes (or zero) in front of ?
        val = re.sub(r"(?<!\\)(\\\\)*(?!\\)\?", r"\1_", val)
        return val

    def generateAggregation(self, agg, where_clausel):
        if not agg:
            return self.table, where_clausel

        if  (agg.aggfunc == SigmaAggregationParser.AGGFUNC_COUNT or
            agg.aggfunc == SigmaAggregationParser.AGGFUNC_MAX or
            agg.aggfunc == SigmaAggregationParser.AGGFUNC_MIN or
            agg.aggfunc == SigmaAggregationParser.AGGFUNC_SUM or
            agg.aggfunc == SigmaAggregationParser.AGGFUNC_AVG):

            if agg.groupfield:
                group_by = " GROUP BY {0}".format(self.fieldNameMapping(agg.groupfield, None))
            else:
                group_by = ""

            if agg.aggfield:
                select = "*,{}({}) AS agg".format(agg.aggfunc_notrans, self.fieldNameMapping(agg.aggfield, None))
            else:
                if agg.aggfunc == SigmaAggregationParser.AGGFUNC_COUNT:
                    select = "*,{}(*) AS agg".format(agg.aggfunc_notrans)
                else:
                    raise SigmaParseError("For {} aggregation a fieldname needs to be specified".format(agg.aggfunc_notrans))

            temp_table = "(SELECT {} FROM {} WHERE {}{})".format(select, self.table, where_clausel, group_by)
            agg_condition =  "agg {} {}".format(agg.cond_op, agg.condition)

            return temp_table, agg_condition

        raise NotImplementedError("{} aggregation not implemented in SQL Backend".format(agg.aggfunc_notrans))
    
    def generateQuery(self, parsed):
        return self._generateQueryWithFields(parsed, list("*"))

    def checkFTS(self, parsed, result):
        if self._recursiveFtsSearch(parsed.parsedSearch):
            raise NotImplementedError("FullTextSearch not implemented for SQL Backend.")

    def _generateQueryWithFields(self, parsed, fields):
        """
        Return a SQL query with fields specified.
        """

        result = self.generateNode(parsed.parsedSearch)

        self.checkFTS(parsed, result)

        select = ", ".join(fields)

        if parsed.parsedAgg:
            #Handle aggregation
            fro, whe = self.generateAggregation(parsed.parsedAgg, result)
            return "SELECT {} FROM {} WHERE {}".format(select, fro, whe)

        return "SELECT {} FROM {} WHERE Timestamp BETWEEN '<time_a>' AND '<time_b>' AND type IN ('windows','agent_os') AND {}".format(select, self.table, result)

    def _recursiveFtsSearch(self, subexpression):
        #True: found subexpression, where no fieldname is requested -> full text search
        #False: no subexpression found, where a full text search is needed

        def _evaluateCondition(condition):
            #Helper function to evaluate conditions
            if type(condition) not in  [ConditionAND, ConditionOR, ConditionNOT]:
                raise NotImplementedError("Error in recursive Search logic")

            results = []
            for elem in condition.items:
                if isinstance(elem, NodeSubexpression):
                    results.append(self._recursiveFtsSearch(elem))
                if isinstance(elem, ConditionNOT):
                    results.append(_evaluateCondition(elem))
                if isinstance(elem, tuple):
                    results.append(False)
                if type(elem) in (str, int, list):
                    return True
            return any(results)

        if type(subexpression) in [str, int, list]:
            return True
        elif type(subexpression) in [tuple]:
            return False

        if not isinstance(subexpression, NodeSubexpression):
            raise NotImplementedError("Error in recursive Search logic")

        if isinstance(subexpression.items, NodeSubexpression):
            return self._recursiveFtsSearch(subexpression.items)
        elif type(subexpression.items) in [ConditionAND, ConditionOR, ConditionNOT]:
            return _evaluateCondition(subexpression.items)
