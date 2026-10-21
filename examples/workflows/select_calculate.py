"""Submit through run_workflow or python -m arcgis_mcp.workflow.

Inputs: features -> a standalone feature class with numeric VALUE field.
Parameters: where -> SQL filter; value -> numeric value assigned to matched rows.
The runtime injects workflow. Original and successful intermediate versions remain.
"""
import arcpy

source = workflow.inputs['features']
where = workflow.parameters['where']
value = workflow.parameters['value']
if not isinstance(value, (int, float)):
    raise ValueError('value must be numeric')

with workflow.step('select', inputs=[source], parameters={'where': where}) as selection:
    arcpy.analysis.Select(source, selection.output(), where)

with workflow.step('calculate', inputs=[selection.result], parameters={'value': value}) as calculation:
    editable = calculation.edit_copy(selection.result)
    arcpy.management.CalculateField(editable, 'VALUE', repr(value), 'PYTHON3')

workflow.set_outputs({'final': calculation.result})
