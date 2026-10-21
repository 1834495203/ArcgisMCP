"""Step body; declare inputs.features and outputs.selected."""
arcpy.analysis.Select(inputs['features'], outputs['selected'], parameters['where'])
count = int(arcpy.management.GetCount(outputs['selected'])[0])
print({'selected_count': count})
if parameters.get('expected_count') is not None:
    step.check('caller expected selected count', count == parameters['expected_count'],
               {'actual': count, 'expected': parameters['expected_count']})
